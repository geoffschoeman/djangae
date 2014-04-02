import logging
import itertools
import warnings

from google.appengine.api import datastore
from google.appengine.api.datastore_types import Key
from google.appengine.ext import db

from django.core.cache import cache

from django.db.models.sql.where import AND
from djangae.indexing import special_indexes_for_column, REQUIRES_SPECIAL_INDEXES, add_special_index


OPERATORS_MAP = {
    'exact': '=',
    'gt': '>',
    'gte': '>=',
    'lt': '<',
    'lte': '<=',

    # The following operators are supported with special code below.
    'isnull': None,
    'in': None,
    'startswith': None,
    'range': None,
    'year': None,
    'gt_and_lt': None, #Special case inequality combined filter
    'iexact': None
}

from django.utils.functional import memoize

def get_field_from_column(model, column):
    #FIXME: memoize this
    for field in model._meta.fields:
        if field.column == column:
            return field
    return None

class SelectCommand(object):
    def __init__(self, connection, query, keys_only=False, all_fields=False):
        opts = query.get_meta()
        if not query.default_ordering:
            self.ordering = query.order_by
        else:
            self.ordering = query.order_by or opts.ordering

        self.queried_fields = []
        if keys_only:
            self.queried_fields = [ opts.pk.column ]
        elif not all_fields:
            for x in query.select:
                if isinstance(x, tuple):
                    #Django < 1.6 compatibility
                    self.queried_fields.append(x[1])
                else:
                    self.queried_fields.append(x.col[1])

        if not self.queried_fields:
            self.queried_fields = [ x.column for x in opts.fields ]

        self.connection = connection
        self.pk_col = opts.pk.column
        self.model = query.model
        self.is_count = query.aggregates
        self.keys_only = False #FIXME: This should be used where possible
        self.included_pks = []
        self.excluded_pks = []
        self.has_inequality_filter = False
        self.all_filters = []
        self.results = None
        self.query_can_never_return_results = False
        self.extra_select = query.extra_select

        projection_fields = []

        if not all_fields:
            for field in self.queried_fields:
                #We don't include the primary key in projection queries...
                if field == self.pk_col:
                    continue

                #Text and byte fields aren't indexed, so we can't do a
                #projection query
                f = get_field_from_column(self.model, field)
                if not f:
                    import ipdb; ipdb.set_trace()
                assert f #If this happens, we have a cross-table select going on! #FIXME
                db_type = f.db_type(connection)

                if db_type in ("bytes", "text"):
                    projection_fields = []
                    break

                projection_fields.append(field)

        self.projection = list(set(projection_fields)) or None
        if opts.parents:
            self.projection = None

        self.where = self.parse_where_and_check_projection(query.where)

        try:
            #If the PK was queried, we switch it in our queried
            #fields store with __key__
            pk_index = self.queried_fields.index(self.pk_col)
            self.queried_fields[pk_index] = "__key__"

            #If the only field queried was the key, then we can do a keys_only
            #query
            self.keys_only = len(self.queried_fields) == 1
        except ValueError:
            pass

    def parse_where_and_check_projection(self, where, negated=False):
        result = []

        if where.negated:
            negated = not negated

        if not negated and where.connector != AND:
            raise DatabaseError("Only AND filters are supported")

        for child in where.children:
            if isinstance(child, tuple):
                constraint, op, annotation, value = child
                if isinstance(value, (list, tuple)):
                    value = [ self.connection.ops.prep_lookup_value(self.model, x, constraint.field) for x in value]
                else:
                    value = self.connection.ops.prep_lookup_value(self.model, value, constraint.field)

                #Disable projection if it's not supported
                if self.projection and constraint.col in self.projection:
                    if op in ("exact", "in", "isnull"):
                        #If we are projecting, but we are doing an
                        #equality filter on one of the columns, then we
                        #can't project
                        self.projection = None


                if negated:
                    if op in ("exact", "in") and constraint.field.primary_key:
                        self.excluded_pks.append(value)
                    #else: FIXME when excluded_pks is handled, we can put the
                    #next section in an else block
                    if op == "exact":
                        if self.has_inequality_filter:
                            raise RuntimeError("You can only specify one inequality filter per query")

                        col = constraint.col
                        result.append((col, "gt_and_lt", value))
                        self.has_inequality_filter = True
                    else:
                        raise RuntimeError("Unsupported negated lookup: " + op)
                else:
                    if constraint.field.primary_key:
                        if (value is None and op == "exact") or op == "isnull":
                            #If we are looking for a primary key that is None, then we always
                            #just return nothing
                            self.query_can_never_return_results = True

                        elif op in ("exact", "in"):
                            if isinstance(value, (list, tuple)):
                                self.included_pks.extend(list(value))
                            else:
                                self.included_pks.append(value)
                    #else: FIXME when included_pks is handled, we can put the
                    #next section in an else block
                    col = constraint.col
                    result.append((col, op, value))
            else:
                result.extend(self.parse_where_and_check_projection(child, negated))

        return result

    def execute(self):
        if self.query_can_never_return_results:
            self.results = []
            return

        combined_filters = []

        inheritance_root = self.model

        concrete_parents = [ x for x in self.model._meta.parents if not x._meta.abstract]

        if concrete_parents:
            for parent in self.model._meta.get_parent_list():
                if not parent._meta.parents:
                    #If this is the top parent, override the db_table
                    inheritance_root = parent

        query = datastore.Query(
            inheritance_root._meta.db_table,
            projection=self.projection
        )

        #Only filter on class if we have some non-abstract parents
        if concrete_parents and not self.model._meta.proxy:
            query["class ="] = self.model._meta.db_table

        logging.info("Select query: {0}, {1}".format(self.model.__name__, self.where))

        for column, op, value in self.where:
            if column == self.pk_col:
                column = "__key__"

            final_op = OPERATORS_MAP.get(op)
            if final_op is None:
                if op in REQUIRES_SPECIAL_INDEXES:
                    add_special_index(self.model, column, op) #Add the index if we can (e.g. on dev_appserver)

                    if op not in special_indexes_for_column(self.model, column):
                        raise RuntimeError("There is a missing index in your djangaeidx.yaml - \n\n{0}:\n\t{1}: [{2}]".format(
                            self.model, column, op)
                        )

                    indexer = REQUIRES_SPECIAL_INDEXES[op]
                    column = indexer.indexed_column_name(column)
                    value = indexer.prep_value_for_query(value)
                    query["%s =" % column] = value
                else:
                    if op == "in":
                        combined_filters.append((column, op, value))
                    elif op == "gt_and_lt":
                        combined_filters.append((column, op, value))
                    elif op == "isnull":
                        query["%s =" % column] = None
                    else:
                        raise NotImplementedError("Unimplemented operator {0}".format(op))
            else:
                query["%s %s" % (column, final_op)] = value


        ordering = []
        for order in self.ordering:
            direction = datastore.Query.DESCENDING if order.startswith("-") else datastore.Query.ASCENDING
            order = order.lstrip("-")
            if order == self.model._meta.pk.column:
                order = "__key__"
            ordering.append((order, direction))



        if combined_filters:
            queries = [ query ]
            for column, op, value in combined_filters:
                new_queries = []
                for query in queries:
                    if op == "in":
                        for val in value:
                            new_query = datastore.Query(self.model._meta.db_table)
                            new_query.update(query)
                            new_query["%s =" % column] = val
                            new_queries.append(new_query)
                    elif op == "gt_and_lt":
                        for tmp_op in ("<", ">"):
                            new_query = datastore.Query(self.model._meta.db_table)
                            new_query.update(query)
                            new_query["%s %s" % (column, tmp_op)] = value
                            new_queries.append(new_query)
                queries = new_queries

            query = datastore.MultiQuery(queries, ordering)
        else:
            query.Order(*ordering)

        #print query
        self.query = query
        self.results = None
        self.query_done = False
        self.aggregate_type = "count" if self.is_count else None
        self._do_fetch()

    def _do_fetch(self):
        assert not self.results

        if isinstance(self.query, datastore.MultiQuery):
            self.results = self._run_query(aggregate_type=self.aggregate_type)
            self.query_done = True
        else:
            #Try and get the entity from the cache, this is to work around HRD issues
            #and boost performance!
            entity_from_cache = None
            if self.all_filters and self.model:
                #Get all the exact filters
                exact_filters = [ x for x in self.all_filters if x[1] == "=" ]
                lookup = { x[0]:x[2] for x in exact_filters }

                unique_combinations = get_uniques_from_model(self.model)
                for fields in unique_combinations:
                    final_key = []
                    for field in fields:
                        if field in lookup:
                            final_key.append((field, lookup[field]))
                            continue
                        else:
                            break
                    else:
                        #We've found a unique combination!
                        unique_key = generate_unique_key(self.model, final_key)
                        entity_from_cache = get_entity_from_cache(unique_key)

            if entity_from_cache is None:
                self.results = self._run_query(aggregate_type=self.aggregate_type)
            else:
                self.results = [ entity_from_cache ]

    def _run_query(self, limit=None, start=None, aggregate_type=None):
        if aggregate_type is None:
            return self.query.Run(limit=limit, start=start)
        elif self.aggregate_type == "count":
            return self.query.Count(limit=limit, start=start)
        else:
            raise RuntimeError("Unsupported query type")

class FlushCommand(object):
    """
        sql_flush returns the SQL statements to flush the database,
        which are then executed by cursor.execute()

        We instead return a list of FlushCommands which are called by
        our cursor.execute
    """
    def __init__(self, table):
        self.table = table

    def execute(self):
        table = self.table
        query = datastore.Query(table, keys_only=True)
        while query.Count():
            datastore.Delete(query.Run())

        cache.clear()

class InsertCommand(object):
    def __init__(self, connection, model, objs, fields, raw):
        from .base import django_instance_to_entity, get_datastore_kind

        self.has_pk = any([x.primary_key for x in fields])
        self.entities = []
        self.included_keys = []
        self.model = model

        for obj in objs:
            if self.has_pk:
                self.included_keys.append(Key.from_path(get_datastore_kind(model), obj.pk))

            self.entities.append(
                django_instance_to_entity(connection, model, fields, raw, obj)
            )

    def execute(self):
        from .base import IntegrityError

        if self.has_pk:
            results = []
            #We are inserting, but we specified an ID, we need to check for existence before we Put()
            for key, ent in zip(self.included_keys, self.entities):
                @db.transactional
                def txn():
                    try:
                        existing = datastore.Get(key)

                        #Djangae's polymodel/inheritance support stores a class attribute containing all of the parents
                        #of the model. Parent classes share the same table as subclasses and so this will incorrectly throw
                        #on the write of the subclass after the parent has been written. So we check here.
                        # If the new entity has a class attribute AND the fields of the existing model are a subset of the
                        # subclass then we assume that we are using inheritance here and don't throw. It's a little ugly...
                        existing_is_parent = ent.get('class') and set(existing.keys()).issubset(ent.keys())
                        if not existing_is_parent:
                            raise IntegrityError("Tried to INSERT with existing key")
                    except db.EntityNotFoundError:
                        pass

                    results.append(datastore.Put(ent))

                txn()

            return results
        else:
            return datastore.Put(self.entities)

class DeleteCommand(object):
    def __init__(self, connection, query):
        self.select = SelectCommand(connection, query, keys_only=True)

    def execute(self):
        self.select.execute()
        datastore.Delete(self.select.results)
        #FIXME: Remove from the cache

class UpdateCommand(object):
    def __init__(self, connection, query):
        self.model = query.model
        self.select = SelectCommand(connection, query, all_fields=True)
        self.values = query.values
        self.connection = connection

    def execute(self):
        from .base import get_prepared_db_value, MockInstance
        from .base import cache_entity

        self.select.execute()

        results = self.select.results
        entities = []
        i = 0
        for result in results:
            i += 1
            for field, param, value in self.values:
                result[field.column] = get_prepared_db_value(self.connection, MockInstance(field, value), field)

                #Add special indexed fields
                for index in special_indexes_for_column(self.model, field.column):
                    indexer = REQUIRES_SPECIAL_INDEXES[index]
                    result[indexer.indexed_column_name(field.column)] = indexer.prep_value_for_database(value)

            entities.append(result)

        returned_ids = datastore.Put(entities)

        model = self.select.model

        #Now cache them, temporarily to help avoid consistency errors
        for key, entity in itertools.izip(returned_ids, entities):
            pk_column = model._meta.pk.column

            #If there are parent models, search the parents for the
            #first primary key which isn't a relation field
            for parent in model._meta.parents.keys():
                if not parent._meta.pk.rel:
                    pk_column = parent._meta.pk.column

            entity[pk_column] = key.id_or_name()
            cache_entity(model, entity)

        return i
