# orm/relationships.py
# Copyright (C) 2005-2020 the SQLAlchemy authors and contributors
# <see AUTHORS file>
#
# This module is part of SQLAlchemy and is released under
# the MIT License: http://www.opensource.org/licenses/mit-license.php

"""Heuristics related to join conditions as used in
:func:`_orm.relationship`.

Provides the :class:`.JoinCondition` object, which encapsulates
SQL annotation and aliasing behavior focused on the `primaryjoin`
and `secondaryjoin` aspects of :func:`_orm.relationship`.

"""
from __future__ import absolute_import

import collections
import re
import weakref

from . import attributes
from .base import state_str
from .interfaces import MANYTOMANY
from .interfaces import MANYTOONE
from .interfaces import ONETOMANY
from .interfaces import PropComparator
from .interfaces import StrategizedProperty
from .util import _orm_annotate
from .util import _orm_deannotate
from .util import CascadeOptions
from .. import exc as sa_exc
from .. import log
from .. import schema
from .. import sql
from .. import util
from ..inspection import inspect
from ..sql import coercions
from ..sql import expression
from ..sql import operators
from ..sql import roles
from ..sql import visitors
from ..sql.util import _deep_deannotate
from ..sql.util import _shallow_annotate
from ..sql.util import adapt_criterion_to_null
from ..sql.util import ClauseAdapter
from ..sql.util import join_condition
from ..sql.util import selectables_overlap
from ..sql.util import visit_binary_product


if util.TYPE_CHECKING:
    from .util import AliasedInsp
    from typing import Union


def remote(expr):
    """Annotate a portion of a primaryjoin expression
    with a 'remote' annotation.

    See the section :ref:`relationship_custom_foreign` for a
    description of use.

    .. seealso::

        :ref:`relationship_custom_foreign`

        :func:`.foreign`

    """
    return _annotate_columns(
        coercions.expect(roles.ColumnArgumentRole, expr), {"remote": True}
    )


def foreign(expr):
    """Annotate a portion of a primaryjoin expression
    with a 'foreign' annotation.

    See the section :ref:`relationship_custom_foreign` for a
    description of use.

    .. seealso::

        :ref:`relationship_custom_foreign`

        :func:`.remote`

    """

    return _annotate_columns(
        coercions.expect(roles.ColumnArgumentRole, expr), {"foreign": True}
    )


@log.class_logger
class RelationshipProperty(StrategizedProperty):
    """Describes an object property that holds a single item or list
    of items that correspond to a related database table.

    Public constructor is the :func:`_orm.relationship` function.

    .. seealso::

        :ref:`relationship_config_toplevel`

    """

    strategy_wildcard_key = "relationship"

    _persistence_only = dict(
        passive_deletes=False,
        passive_updates=True,
        enable_typechecks=True,
        active_history=False,
        cascade_backrefs=True,
    )

    _dependency_processor = None

    def __init__(
        self,
        argument,
        secondary=None,
        primaryjoin=None,
        secondaryjoin=None,
        foreign_keys=None,
        uselist=None,
        order_by=False,
        backref=None,
        back_populates=None,
        overlaps=None,
        post_update=False,
        cascade=False,
        viewonly=False,
        lazy="select",
        collection_class=None,
        passive_deletes=_persistence_only["passive_deletes"],
        passive_updates=_persistence_only["passive_updates"],
        remote_side=None,
        enable_typechecks=_persistence_only["enable_typechecks"],
        join_depth=None,
        comparator_factory=None,
        single_parent=False,
        innerjoin=False,
        distinct_target_key=None,
        doc=None,
        active_history=_persistence_only["active_history"],
        cascade_backrefs=_persistence_only["cascade_backrefs"],
        load_on_pending=False,
        bake_queries=True,
        _local_remote_pairs=None,
        query_class=None,
        info=None,
        omit_join=None,
        sync_backref=None,
    ):
        """Provide a relationship between two mapped classes.

        This corresponds to a parent-child or associative table relationship.
        The constructed class is an instance of
        :class:`.RelationshipProperty`.

        A typical :func:`_orm.relationship`, used in a classical mapping::

           mapper(Parent, properties={
             'children': relationship(Child)
           })

        Some arguments accepted by :func:`_orm.relationship`
        optionally accept a
        callable function, which when called produces the desired value.
        The callable is invoked by the parent :class:`_orm.Mapper` at "mapper
        initialization" time, which happens only when mappers are first used,
        and is assumed to be after all mappings have been constructed.  This
        can be used to resolve order-of-declaration and other dependency
        issues, such as if ``Child`` is declared below ``Parent`` in the same
        file::

            mapper(Parent, properties={
                "children":relationship(lambda: Child,
                                    order_by=lambda: Child.id)
            })

        When using the :ref:`declarative_toplevel` extension, the Declarative
        initializer allows string arguments to be passed to
        :func:`_orm.relationship`.  These string arguments are converted into
        callables that evaluate the string as Python code, using the
        Declarative class-registry as a namespace.  This allows the lookup of
        related classes to be automatic via their string name, and removes the
        need for related classes to be imported into the local module space
        before the dependent classes have been declared.  It is still required
        that the modules in which these related classes appear are imported
        anywhere in the application at some point before the related mappings
        are actually used, else a lookup error will be raised when the
        :func:`_orm.relationship`
        attempts to resolve the string reference to the
        related class.    An example of a string- resolved class is as
        follows::

            from sqlalchemy.ext.declarative import declarative_base

            Base = declarative_base()

            class Parent(Base):
                __tablename__ = 'parent'
                id = Column(Integer, primary_key=True)
                children = relationship("Child", order_by="Child.id")

        .. seealso::

          :ref:`relationship_config_toplevel` - Full introductory and
          reference documentation for :func:`_orm.relationship`.

          :ref:`orm_tutorial_relationship` - ORM tutorial introduction.

        :param argument:
          A mapped class, or actual :class:`_orm.Mapper` instance,
          representing
          the target of the relationship.

          :paramref:`_orm.relationship.argument`
          may also be passed as a callable
          function which is evaluated at mapper initialization time, and may
          be passed as a string name when using Declarative.

          .. warning:: Prior to SQLAlchemy 1.3.16, this value is interpreted
             using Python's ``eval()`` function.
             **DO NOT PASS UNTRUSTED INPUT TO THIS STRING**.
             See :ref:`declarative_relationship_eval` for details on
             declarative evaluation of :func:`_orm.relationship` arguments.

          .. versionchanged 1.3.16::

             The string evaluation of the main "argument" no longer accepts an
             open ended Python expression, instead only accepting a string
             class name or dotted package-qualified name.

          .. seealso::

            :ref:`declarative_configuring_relationships` - further detail
            on relationship configuration when using Declarative.

        :param secondary:
          For a many-to-many relationship, specifies the intermediary
          table, and is typically an instance of :class:`_schema.Table`.
          In less common circumstances, the argument may also be specified
          as an :class:`_expression.Alias` construct, or even a
          :class:`_expression.Join` construct.

          :paramref:`_orm.relationship.secondary` may
          also be passed as a callable function which is evaluated at
          mapper initialization time.  When using Declarative, it may also
          be a string argument noting the name of a :class:`_schema.Table`
          that is
          present in the :class:`_schema.MetaData`
          collection associated with the
          parent-mapped :class:`_schema.Table`.

          .. warning:: When passed as a Python-evaluable string, the
             argument is interpreted using Python's ``eval()`` function.
             **DO NOT PASS UNTRUSTED INPUT TO THIS STRING**.
             See :ref:`declarative_relationship_eval` for details on
             declarative evaluation of :func:`_orm.relationship` arguments.

          The :paramref:`_orm.relationship.secondary` keyword argument is
          typically applied in the case where the intermediary
          :class:`_schema.Table`
          is not otherwise expressed in any direct class mapping. If the
          "secondary" table is also explicitly mapped elsewhere (e.g. as in
          :ref:`association_pattern`), one should consider applying the
          :paramref:`_orm.relationship.viewonly` flag so that this
          :func:`_orm.relationship`
          is not used for persistence operations which
          may conflict with those of the association object pattern.

          .. seealso::

              :ref:`relationships_many_to_many` - Reference example of "many
              to many".

              :ref:`orm_tutorial_many_to_many` - ORM tutorial introduction to
              many-to-many relationships.

              :ref:`self_referential_many_to_many` - Specifics on using
              many-to-many in a self-referential case.

              :ref:`declarative_many_to_many` - Additional options when using
              Declarative.

              :ref:`association_pattern` - an alternative to
              :paramref:`_orm.relationship.secondary`
              when composing association
              table relationships, allowing additional attributes to be
              specified on the association table.

              :ref:`composite_secondary_join` - a lesser-used pattern which
              in some cases can enable complex :func:`_orm.relationship` SQL
              conditions to be used.

          .. versionadded:: 0.9.2 :paramref:`_orm.relationship.secondary`
             works
             more effectively when referring to a :class:`_expression.Join`
             instance.

        :param active_history=False:
          When ``True``, indicates that the "previous" value for a
          many-to-one reference should be loaded when replaced, if
          not already loaded. Normally, history tracking logic for
          simple many-to-ones only needs to be aware of the "new"
          value in order to perform a flush. This flag is available
          for applications that make use of
          :func:`.attributes.get_history` which also need to know
          the "previous" value of the attribute.

        :param backref:
          Indicates the string name of a property to be placed on the related
          mapper's class that will handle this relationship in the other
          direction. The other property will be created automatically
          when the mappers are configured.  Can also be passed as a
          :func:`.backref` object to control the configuration of the
          new relationship.

          .. seealso::

            :ref:`relationships_backref` - Introductory documentation and
            examples.

            :paramref:`_orm.relationship.back_populates` - alternative form
            of backref specification.

            :func:`.backref` - allows control over :func:`_orm.relationship`
            configuration when using :paramref:`_orm.relationship.backref`.


        :param back_populates:
          Takes a string name and has the same meaning as
          :paramref:`_orm.relationship.backref`, except the complementing
          property is **not** created automatically, and instead must be
          configured explicitly on the other mapper.  The complementing
          property should also indicate
          :paramref:`_orm.relationship.back_populates` to this relationship to
          ensure proper functioning.

          .. seealso::

            :ref:`relationships_backref` - Introductory documentation and
            examples.

            :paramref:`_orm.relationship.backref` - alternative form
            of backref specification.

        :param overlaps:
           A string name or comma-delimited set of names of other relationships
           on either this mapper, a descendant mapper, or a target mapper with
           which this relationship may write to the same foreign keys upon
           persistence.   The only effect this has is to eliminate the
           warning that this relationship will conflict with another upon
           persistence.   This is used for such relationships that are truly
           capable of conflicting with each other on write, but the application
           will ensure that no such conflicts occur.

           .. versionadded:: 1.4

        :param bake_queries=True:
          Use the :class:`.BakedQuery` cache to cache the construction of SQL
          used in lazy loads.  True by default.   Set to False if the
          join condition of the relationship has unusual features that
          might not respond well to statement caching.

          .. versionchanged:: 1.2
             "Baked" loading is the default implementation for the "select",
             a.k.a. "lazy" loading strategy for relationships.

          .. versionadded:: 1.0.0

          .. seealso::

            :ref:`baked_toplevel`

        :param cascade:
          A comma-separated list of cascade rules which determines how
          Session operations should be "cascaded" from parent to child.
          This defaults to ``False``, which means the default cascade
          should be used - this default cascade is ``"save-update, merge"``.

          The available cascades are ``save-update``, ``merge``,
          ``expunge``, ``delete``, ``delete-orphan``, and ``refresh-expire``.
          An additional option, ``all`` indicates shorthand for
          ``"save-update, merge, refresh-expire,
          expunge, delete"``, and is often used as in ``"all, delete-orphan"``
          to indicate that related objects should follow along with the
          parent object in all cases, and be deleted when de-associated.

          .. seealso::

            :ref:`unitofwork_cascades` - Full detail on each of the available
            cascade options.

            :ref:`tutorial_delete_cascade` - Tutorial example describing
            a delete cascade.

        :param cascade_backrefs=True:
          A boolean value indicating if the ``save-update`` cascade should
          operate along an assignment event intercepted by a backref.
          When set to ``False``, the attribute managed by this relationship
          will not cascade an incoming transient object into the session of a
          persistent parent, if the event is received via backref.

          .. seealso::

            :ref:`backref_cascade` - Full discussion and examples on how
            the :paramref:`_orm.relationship.cascade_backrefs` option is used.

        :param collection_class:
          A class or callable that returns a new list-holding object. will
          be used in place of a plain list for storing elements.

          .. seealso::

            :ref:`custom_collections` - Introductory documentation and
            examples.

        :param comparator_factory:
          A class which extends :class:`.RelationshipProperty.Comparator`
          which provides custom SQL clause generation for comparison
          operations.

          .. seealso::

            :class:`.PropComparator` - some detail on redefining comparators
            at this level.

            :ref:`custom_comparators` - Brief intro to this feature.


        :param distinct_target_key=None:
          Indicate if a "subquery" eager load should apply the DISTINCT
          keyword to the innermost SELECT statement.  When left as ``None``,
          the DISTINCT keyword will be applied in those cases when the target
          columns do not comprise the full primary key of the target table.
          When set to ``True``, the DISTINCT keyword is applied to the
          innermost SELECT unconditionally.

          It may be desirable to set this flag to False when the DISTINCT is
          reducing performance of the innermost subquery beyond that of what
          duplicate innermost rows may be causing.

          .. versionchanged:: 0.9.0 -
             :paramref:`_orm.relationship.distinct_target_key` now defaults to
             ``None``, so that the feature enables itself automatically for
             those cases where the innermost query targets a non-unique
             key.

          .. seealso::

            :ref:`loading_toplevel` - includes an introduction to subquery
            eager loading.

        :param doc:
          Docstring which will be applied to the resulting descriptor.

        :param foreign_keys:

          A list of columns which are to be used as "foreign key"
          columns, or columns which refer to the value in a remote
          column, within the context of this :func:`_orm.relationship`
          object's :paramref:`_orm.relationship.primaryjoin` condition.
          That is, if the :paramref:`_orm.relationship.primaryjoin`
          condition of this :func:`_orm.relationship` is ``a.id ==
          b.a_id``, and the values in ``b.a_id`` are required to be
          present in ``a.id``, then the "foreign key" column of this
          :func:`_orm.relationship` is ``b.a_id``.

          In normal cases, the :paramref:`_orm.relationship.foreign_keys`
          parameter is **not required.** :func:`_orm.relationship` will
          automatically determine which columns in the
          :paramref:`_orm.relationship.primaryjoin` condition are to be
          considered "foreign key" columns based on those
          :class:`_schema.Column` objects that specify
          :class:`_schema.ForeignKey`,
          or are otherwise listed as referencing columns in a
          :class:`_schema.ForeignKeyConstraint` construct.
          :paramref:`_orm.relationship.foreign_keys` is only needed when:

            1. There is more than one way to construct a join from the local
               table to the remote table, as there are multiple foreign key
               references present.  Setting ``foreign_keys`` will limit the
               :func:`_orm.relationship`
               to consider just those columns specified
               here as "foreign".

            2. The :class:`_schema.Table` being mapped does not actually have
               :class:`_schema.ForeignKey` or
               :class:`_schema.ForeignKeyConstraint`
               constructs present, often because the table
               was reflected from a database that does not support foreign key
               reflection (MySQL MyISAM).

            3. The :paramref:`_orm.relationship.primaryjoin`
               argument is used to
               construct a non-standard join condition, which makes use of
               columns or expressions that do not normally refer to their
               "parent" column, such as a join condition expressed by a
               complex comparison using a SQL function.

          The :func:`_orm.relationship` construct will raise informative
          error messages that suggest the use of the
          :paramref:`_orm.relationship.foreign_keys` parameter when
          presented with an ambiguous condition.   In typical cases,
          if :func:`_orm.relationship` doesn't raise any exceptions, the
          :paramref:`_orm.relationship.foreign_keys` parameter is usually
          not needed.

          :paramref:`_orm.relationship.foreign_keys` may also be passed as a
          callable function which is evaluated at mapper initialization time,
          and may be passed as a Python-evaluable string when using
          Declarative.

          .. warning:: When passed as a Python-evaluable string, the
             argument is interpreted using Python's ``eval()`` function.
             **DO NOT PASS UNTRUSTED INPUT TO THIS STRING**.
             See :ref:`declarative_relationship_eval` for details on
             declarative evaluation of :func:`_orm.relationship` arguments.

          .. seealso::

            :ref:`relationship_foreign_keys`

            :ref:`relationship_custom_foreign`

            :func:`.foreign` - allows direct annotation of the "foreign"
            columns within a :paramref:`_orm.relationship.primaryjoin`
            condition.

        :param info: Optional data dictionary which will be populated into the
            :attr:`.MapperProperty.info` attribute of this object.

        :param innerjoin=False:
          When ``True``, joined eager loads will use an inner join to join
          against related tables instead of an outer join.  The purpose
          of this option is generally one of performance, as inner joins
          generally perform better than outer joins.

          This flag can be set to ``True`` when the relationship references an
          object via many-to-one using local foreign keys that are not
          nullable, or when the reference is one-to-one or a collection that
          is guaranteed to have one or at least one entry.

          The option supports the same "nested" and "unnested" options as
          that of :paramref:`_orm.joinedload.innerjoin`.  See that flag
          for details on nested / unnested behaviors.

          .. seealso::

            :paramref:`_orm.joinedload.innerjoin` - the option as specified by
            loader option, including detail on nesting behavior.

            :ref:`what_kind_of_loading` - Discussion of some details of
            various loader options.


        :param join_depth:
          When non-``None``, an integer value indicating how many levels
          deep "eager" loaders should join on a self-referring or cyclical
          relationship.  The number counts how many times the same Mapper
          shall be present in the loading condition along a particular join
          branch.  When left at its default of ``None``, eager loaders
          will stop chaining when they encounter a the same target mapper
          which is already higher up in the chain.  This option applies
          both to joined- and subquery- eager loaders.

          .. seealso::

            :ref:`self_referential_eager_loading` - Introductory documentation
            and examples.

        :param lazy='select': specifies
          How the related items should be loaded.  Default value is
          ``select``.  Values include:

          * ``select`` - items should be loaded lazily when the property is
            first accessed, using a separate SELECT statement, or identity map
            fetch for simple many-to-one references.

          * ``immediate`` - items should be loaded as the parents are loaded,
            using a separate SELECT statement, or identity map fetch for
            simple many-to-one references.

          * ``joined`` - items should be loaded "eagerly" in the same query as
            that of the parent, using a JOIN or LEFT OUTER JOIN.  Whether
            the join is "outer" or not is determined by the
            :paramref:`_orm.relationship.innerjoin` parameter.

          * ``subquery`` - items should be loaded "eagerly" as the parents are
            loaded, using one additional SQL statement, which issues a JOIN to
            a subquery of the original statement, for each collection
            requested.

          * ``selectin`` - items should be loaded "eagerly" as the parents
            are loaded, using one or more additional SQL statements, which
            issues a JOIN to the immediate parent object, specifying primary
            key identifiers using an IN clause.

            .. versionadded:: 1.2

          * ``noload`` - no loading should occur at any time.  This is to
            support "write-only" attributes, or attributes which are
            populated in some manner specific to the application.

          * ``raise`` - lazy loading is disallowed; accessing
            the attribute, if its value were not already loaded via eager
            loading, will raise an :exc:`~sqlalchemy.exc.InvalidRequestError`.
            This strategy can be used when objects are to be detached from
            their attached :class:`.Session` after they are loaded.

            .. versionadded:: 1.1

          * ``raise_on_sql`` - lazy loading that emits SQL is disallowed;
            accessing the attribute, if its value were not already loaded via
            eager loading, will raise an
            :exc:`~sqlalchemy.exc.InvalidRequestError`, **if the lazy load
            needs to emit SQL**.  If the lazy load can pull the related value
            from the identity map or determine that it should be None, the
            value is loaded.  This strategy can be used when objects will
            remain associated with the attached :class:`.Session`, however
            additional SELECT statements should be blocked.

            .. versionadded:: 1.1

          * ``dynamic`` - the attribute will return a pre-configured
            :class:`_query.Query` object for all read
            operations, onto which further filtering operations can be
            applied before iterating the results.  See
            the section :ref:`dynamic_relationship` for more details.

          * True - a synonym for 'select'

          * False - a synonym for 'joined'

          * None - a synonym for 'noload'

          .. seealso::

            :doc:`/orm/loading_relationships` - Full documentation on
            relationship loader configuration.

            :ref:`dynamic_relationship` - detail on the ``dynamic`` option.

            :ref:`collections_noload_raiseload` - notes on "noload" and "raise"

        :param load_on_pending=False:
          Indicates loading behavior for transient or pending parent objects.

          When set to ``True``, causes the lazy-loader to
          issue a query for a parent object that is not persistent, meaning it
          has never been flushed.  This may take effect for a pending object
          when autoflush is disabled, or for a transient object that has been
          "attached" to a :class:`.Session` but is not part of its pending
          collection.

          The :paramref:`_orm.relationship.load_on_pending`
          flag does not improve
          behavior when the ORM is used normally - object references should be
          constructed at the object level, not at the foreign key level, so
          that they are present in an ordinary way before a flush proceeds.
          This flag is not not intended for general use.

          .. seealso::

              :meth:`.Session.enable_relationship_loading` - this method
              establishes "load on pending" behavior for the whole object, and
              also allows loading on objects that remain transient or
              detached.

        :param order_by:
          Indicates the ordering that should be applied when loading these
          items.  :paramref:`_orm.relationship.order_by`
          is expected to refer to
          one of the :class:`_schema.Column`
          objects to which the target class is
          mapped, or the attribute itself bound to the target class which
          refers to the column.

          :paramref:`_orm.relationship.order_by`
          may also be passed as a callable
          function which is evaluated at mapper initialization time, and may
          be passed as a Python-evaluable string when using Declarative.

          .. warning:: When passed as a Python-evaluable string, the
             argument is interpreted using Python's ``eval()`` function.
             **DO NOT PASS UNTRUSTED INPUT TO THIS STRING**.
             See :ref:`declarative_relationship_eval` for details on
             declarative evaluation of :func:`_orm.relationship` arguments.

        :param passive_deletes=False:
           Indicates loading behavior during delete operations.

           A value of True indicates that unloaded child items should not
           be loaded during a delete operation on the parent.  Normally,
           when a parent item is deleted, all child items are loaded so
           that they can either be marked as deleted, or have their
           foreign key to the parent set to NULL.  Marking this flag as
           True usually implies an ON DELETE <CASCADE|SET NULL> rule is in
           place which will handle updating/deleting child rows on the
           database side.

           Additionally, setting the flag to the string value 'all' will
           disable the "nulling out" of the child foreign keys, when the parent
           object is deleted and there is no delete or delete-orphan cascade
           enabled.  This is typically used when a triggering or error raise
           scenario is in place on the database side.  Note that the foreign
           key attributes on in-session child objects will not be changed after
           a flush occurs so this is a very special use-case setting.
           Additionally, the "nulling out" will still occur if the child
           object is de-associated with the parent.

           .. seealso::

                :ref:`passive_deletes` - Introductory documentation
                and examples.

        :param passive_updates=True:
          Indicates the persistence behavior to take when a referenced
          primary key value changes in place, indicating that the referencing
          foreign key columns will also need their value changed.

          When True, it is assumed that ``ON UPDATE CASCADE`` is configured on
          the foreign key in the database, and that the database will
          handle propagation of an UPDATE from a source column to
          dependent rows.  When False, the SQLAlchemy
          :func:`_orm.relationship`
          construct will attempt to emit its own UPDATE statements to
          modify related targets.  However note that SQLAlchemy **cannot**
          emit an UPDATE for more than one level of cascade.  Also,
          setting this flag to False is not compatible in the case where
          the database is in fact enforcing referential integrity, unless
          those constraints are explicitly "deferred", if the target backend
          supports it.

          It is highly advised that an application which is employing
          mutable primary keys keeps ``passive_updates`` set to True,
          and instead uses the referential integrity features of the database
          itself in order to handle the change efficiently and fully.

          .. seealso::

              :ref:`passive_updates` - Introductory documentation and
              examples.

              :paramref:`.mapper.passive_updates` - a similar flag which
              takes effect for joined-table inheritance mappings.

        :param post_update:
          This indicates that the relationship should be handled by a
          second UPDATE statement after an INSERT or before a
          DELETE. Currently, it also will issue an UPDATE after the
          instance was UPDATEd as well, although this technically should
          be improved. This flag is used to handle saving bi-directional
          dependencies between two individual rows (i.e. each row
          references the other), where it would otherwise be impossible to
          INSERT or DELETE both rows fully since one row exists before the
          other. Use this flag when a particular mapping arrangement will
          incur two rows that are dependent on each other, such as a table
          that has a one-to-many relationship to a set of child rows, and
          also has a column that references a single child row within that
          list (i.e. both tables contain a foreign key to each other). If
          a flush operation returns an error that a "cyclical
          dependency" was detected, this is a cue that you might want to
          use :paramref:`_orm.relationship.post_update` to "break" the cycle.

          .. seealso::

              :ref:`post_update` - Introductory documentation and examples.

        :param primaryjoin:
          A SQL expression that will be used as the primary
          join of the child object against the parent object, or in a
          many-to-many relationship the join of the parent object to the
          association table. By default, this value is computed based on the
          foreign key relationships of the parent and child tables (or
          association table).

          :paramref:`_orm.relationship.primaryjoin` may also be passed as a
          callable function which is evaluated at mapper initialization time,
          and may be passed as a Python-evaluable string when using
          Declarative.

          .. warning:: When passed as a Python-evaluable string, the
             argument is interpreted using Python's ``eval()`` function.
             **DO NOT PASS UNTRUSTED INPUT TO THIS STRING**.
             See :ref:`declarative_relationship_eval` for details on
             declarative evaluation of :func:`_orm.relationship` arguments.

          .. seealso::

              :ref:`relationship_primaryjoin`

        :param remote_side:
          Used for self-referential relationships, indicates the column or
          list of columns that form the "remote side" of the relationship.

          :paramref:`_orm.relationship.remote_side` may also be passed as a
          callable function which is evaluated at mapper initialization time,
          and may be passed as a Python-evaluable string when using
          Declarative.

          .. warning:: When passed as a Python-evaluable string, the
             argument is interpreted using Python's ``eval()`` function.
             **DO NOT PASS UNTRUSTED INPUT TO THIS STRING**.
             See :ref:`declarative_relationship_eval` for details on
             declarative evaluation of :func:`_orm.relationship` arguments.

          .. seealso::

            :ref:`self_referential` - in-depth explanation of how
            :paramref:`_orm.relationship.remote_side`
            is used to configure self-referential relationships.

            :func:`.remote` - an annotation function that accomplishes the
            same purpose as :paramref:`_orm.relationship.remote_side`,
            typically
            when a custom :paramref:`_orm.relationship.primaryjoin` condition
            is used.

        :param query_class:
          A :class:`_query.Query`
          subclass that will be used as the base of the
          "appender query" returned by a "dynamic" relationship, that
          is, a relationship that specifies ``lazy="dynamic"`` or was
          otherwise constructed using the :func:`_orm.dynamic_loader`
          function.

          .. seealso::

            :ref:`dynamic_relationship` - Introduction to "dynamic"
            relationship loaders.

        :param secondaryjoin:
          A SQL expression that will be used as the join of
          an association table to the child object. By default, this value is
          computed based on the foreign key relationships of the association
          and child tables.

          :paramref:`_orm.relationship.secondaryjoin` may also be passed as a
          callable function which is evaluated at mapper initialization time,
          and may be passed as a Python-evaluable string when using
          Declarative.

          .. warning:: When passed as a Python-evaluable string, the
             argument is interpreted using Python's ``eval()`` function.
             **DO NOT PASS UNTRUSTED INPUT TO THIS STRING**.
             See :ref:`declarative_relationship_eval` for details on
             declarative evaluation of :func:`_orm.relationship` arguments.

          .. seealso::

              :ref:`relationship_primaryjoin`

        :param single_parent:
          When True, installs a validator which will prevent objects
          from being associated with more than one parent at a time.
          This is used for many-to-one or many-to-many relationships that
          should be treated either as one-to-one or one-to-many.  Its usage
          is optional, except for :func:`_orm.relationship` constructs which
          are many-to-one or many-to-many and also
          specify the ``delete-orphan`` cascade option.  The
          :func:`_orm.relationship` construct itself will raise an error
          instructing when this option is required.

          .. seealso::

            :ref:`unitofwork_cascades` - includes detail on when the
            :paramref:`_orm.relationship.single_parent`
            flag may be appropriate.

        :param uselist:
          A boolean that indicates if this property should be loaded as a
          list or a scalar. In most cases, this value is determined
          automatically by :func:`_orm.relationship` at mapper configuration
          time, based on the type and direction
          of the relationship - one to many forms a list, many to one
          forms a scalar, many to many is a list. If a scalar is desired
          where normally a list would be present, such as a bi-directional
          one-to-one relationship, set :paramref:`_orm.relationship.uselist`
          to
          False.

          The :paramref:`_orm.relationship.uselist`
          flag is also available on an
          existing :func:`_orm.relationship`
          construct as a read-only attribute,
          which can be used to determine if this :func:`_orm.relationship`
          deals
          with collections or scalar attributes::

              >>> User.addresses.property.uselist
              True

          .. seealso::

              :ref:`relationships_one_to_one` - Introduction to the "one to
              one" relationship pattern, which is typically when the
              :paramref:`_orm.relationship.uselist` flag is needed.

        :param viewonly=False:
          When set to ``True``, the relationship is used only for loading
          objects, and not for any persistence operation.  A
          :func:`_orm.relationship` which specifies
          :paramref:`_orm.relationship.viewonly` can work
          with a wider range of SQL operations within the
          :paramref:`_orm.relationship.primaryjoin` condition, including
          operations that feature the use of a variety of comparison operators
          as well as SQL functions such as :func:`_expression.cast`.  The
          :paramref:`_orm.relationship.viewonly`
          flag is also of general use when defining any kind of
          :func:`_orm.relationship` that doesn't represent
          the full set of related objects, to prevent modifications of the
          collection from resulting in persistence operations.

          When using the :paramref:`_orm.relationship.viewonly` flag in
          conjunction with backrefs, the
          :paramref:`_orm.relationship.sync_backref` should be set to False;
          this indicates that the backref should not actually populate this
          relationship with data when changes occur on the other side; as this
          is a viewonly relationship, it cannot accommodate changes in state
          correctly as these will not be persisted.

          .. versionadded:: 1.3.17 - the
             :paramref:`_orm.relationship.sync_backref`
             flag set to False is required when using viewonly in conjunction
             with backrefs. A warning is emitted when this flag is not set.

          .. seealso::

            :paramref:`_orm.relationship.sync_backref`

        :param sync_backref:
          A boolean that enables the events used to synchronize the in-Python
          attributes when this relationship is target of either
          :paramref:`_orm.relationship.backref` or
          :paramref:`_orm.relationship.back_populates`.

          Defaults to ``None``, which indicates that an automatic value should
          be selected based on the value of the
          :paramref:`_orm.relationship.viewonly` flag.  When left at its
          default, changes in state for writable relationships will be
          back-populated normally.  For viewonly relationships, a warning is
          emitted unless the flag is set to ``False``.

          .. versionadded:: 1.3.17

          .. seealso::

            :paramref:`_orm.relationship.viewonly`

        :param omit_join:
          Allows manual control over the "selectin" automatic join
          optimization.  Set to ``False`` to disable the "omit join" feature
          added in SQLAlchemy 1.3; or leave as ``None`` to leave automatic
          optimization in place.

          .. note:: This flag may only be set to ``False``.   It is not
             necessary to set it to ``True`` as the "omit_join" optimization is
             automatically detected; if it is not detected, then the
             optimization is not supported.

             .. versionchanged:: 1.3.11  setting ``omit_join`` to True will now
                emit a warning as this was not the intended use of this flag.

          .. versionadded:: 1.3


        """
        super(RelationshipProperty, self).__init__()

        self.uselist = uselist
        self.argument = argument
        self.secondary = secondary
        self.primaryjoin = primaryjoin
        self.secondaryjoin = secondaryjoin
        self.post_update = post_update
        self.direction = None
        self.viewonly = viewonly
        if viewonly:
            self._warn_for_persistence_only_flags(
                passive_deletes=passive_deletes,
                passive_updates=passive_updates,
                enable_typechecks=enable_typechecks,
                active_history=active_history,
                cascade_backrefs=cascade_backrefs,
            )
        if viewonly and sync_backref:
            raise sa_exc.ArgumentError(
                "sync_backref and viewonly cannot both be True"
            )
        self.sync_backref = sync_backref
        self.lazy = lazy
        self.single_parent = single_parent
        self._user_defined_foreign_keys = foreign_keys
        self.collection_class = collection_class
        self.passive_deletes = passive_deletes
        self.cascade_backrefs = cascade_backrefs
        self.passive_updates = passive_updates
        self.remote_side = remote_side
        self.enable_typechecks = enable_typechecks
        self.query_class = query_class
        self.innerjoin = innerjoin
        self.distinct_target_key = distinct_target_key
        self.doc = doc
        self.active_history = active_history
        self.join_depth = join_depth
        if omit_join:
            util.warn(
                "setting omit_join to True is not supported; selectin "
                "loading of this relationship may not work correctly if this "
                "flag is set explicitly.  omit_join optimization is "
                "automatically detected for conditions under which it is "
                "supported."
            )

        self.omit_join = omit_join
        self.local_remote_pairs = _local_remote_pairs
        self.bake_queries = bake_queries
        self.load_on_pending = load_on_pending
        self.comparator_factory = (
            comparator_factory or RelationshipProperty.Comparator
        )
        self.comparator = self.comparator_factory(self, None)
        util.set_creation_order(self)

        if info is not None:
            self.info = info

        self.strategy_key = (("lazy", self.lazy),)

        self._reverse_property = set()
        if overlaps:
            self._overlaps = set(re.split(r"\s*,\s*", overlaps))
        else:
            self._overlaps = ()

        if cascade is not False:
            self.cascade = cascade
        elif self.viewonly:
            self.cascade = "none"
        else:
            self.cascade = "save-update, merge"

        self.order_by = order_by

        self.back_populates = back_populates

        if self.back_populates:
            if backref:
                raise sa_exc.ArgumentError(
                    "backref and back_populates keyword arguments "
                    "are mutually exclusive"
                )
            self.backref = None
        else:
            self.backref = backref

    def _warn_for_persistence_only_flags(self, **kw):
        for k, v in kw.items():
            if v != self._persistence_only[k]:
                # we are warning here rather than warn deprecated as this is a
                # configuration mistake, and Python shows regular warnings more
                # aggressively than deprecation warnings by default. Unlike the
                # case of setting viewonly with cascade, the settings being
                # warned about here are not actively doing the wrong thing
                # against viewonly=True, so it is not as urgent to have these
                # raise an error.
                util.warn(
                    "Setting %s on relationship() while also "
                    "setting viewonly=True does not make sense, as a "
                    "viewonly=True relationship does not perform persistence "
                    "operations. This configuration may raise an error "
                    "in a future release." % (k,)
                )

    def instrument_class(self, mapper):
        attributes.register_descriptor(
            mapper.class_,
            self.key,
            comparator=self.comparator_factory(self, mapper),
            parententity=mapper,
            doc=self.doc,
        )

    class Comparator(PropComparator):
        """Produce boolean, comparison, and other operators for
        :class:`.RelationshipProperty` attributes.

        See the documentation for :class:`.PropComparator` for a brief
        overview of ORM level operator definition.

        .. seealso::

            :class:`.PropComparator`

            :class:`.ColumnProperty.Comparator`

            :class:`.ColumnOperators`

            :ref:`types_operators`

            :attr:`.TypeEngine.comparator_factory`

        """

        _of_type = None

        def __init__(
            self, prop, parentmapper, adapt_to_entity=None, of_type=None
        ):
            """Construction of :class:`.RelationshipProperty.Comparator`
            is internal to the ORM's attribute mechanics.

            """
            self.prop = prop
            self._parententity = parentmapper
            self._adapt_to_entity = adapt_to_entity
            if of_type:
                self._of_type = of_type

        def adapt_to_entity(self, adapt_to_entity):
            return self.__class__(
                self.property,
                self._parententity,
                adapt_to_entity=adapt_to_entity,
                of_type=self._of_type,
            )

        @util.memoized_property
        def entity(self):
            """The target entity referred to by this
            :class:`.RelationshipProperty.Comparator`.

            This is either a :class:`_orm.Mapper` or :class:`.AliasedInsp`
            object.

            This is the "target" or "remote" side of the
            :func:`_orm.relationship`.

            """
            return self.property.entity

        @util.memoized_property
        def mapper(self):
            """The target :class:`_orm.Mapper` referred to by this
            :class:`.RelationshipProperty.Comparator`.

            This is the "target" or "remote" side of the
            :func:`_orm.relationship`.

            """
            return self.property.mapper

        @util.memoized_property
        def _parententity(self):
            return self.property.parent

        def _source_selectable(self):
            if self._adapt_to_entity:
                return self._adapt_to_entity.selectable
            else:
                return self.property.parent._with_polymorphic_selectable

        def __clause_element__(self):
            adapt_from = self._source_selectable()
            if self._of_type:
                of_type_entity = inspect(self._of_type)
            else:
                of_type_entity = None

            (
                pj,
                sj,
                source,
                dest,
                secondary,
                target_adapter,
            ) = self.property._create_joins(
                source_selectable=adapt_from,
                source_polymorphic=True,
                of_type_entity=of_type_entity,
                alias_secondary=True,
            )
            if sj is not None:
                return pj & sj
            else:
                return pj

        def of_type(self, cls):
            r"""Redefine this object in terms of a polymorphic subclass.

            See :meth:`.PropComparator.of_type` for an example.

            """
            return RelationshipProperty.Comparator(
                self.property,
                self._parententity,
                adapt_to_entity=self._adapt_to_entity,
                of_type=cls,
            )

        def in_(self, other):
            """Produce an IN clause - this is not implemented
            for :func:`_orm.relationship`-based attributes at this time.

            """
            raise NotImplementedError(
                "in_() not yet supported for "
                "relationships.  For a simple "
                "many-to-one, use in_() against "
                "the set of foreign key values."
            )

        __hash__ = None

        def __eq__(self, other):
            """Implement the ``==`` operator.

            In a many-to-one context, such as::

              MyClass.some_prop == <some object>

            this will typically produce a
            clause such as::

              mytable.related_id == <some id>

            Where ``<some id>`` is the primary key of the given
            object.

            The ``==`` operator provides partial functionality for non-
            many-to-one comparisons:

            * Comparisons against collections are not supported.
              Use :meth:`~.RelationshipProperty.Comparator.contains`.
            * Compared to a scalar one-to-many, will produce a
              clause that compares the target columns in the parent to
              the given target.
            * Compared to a scalar many-to-many, an alias
              of the association table will be rendered as
              well, forming a natural join that is part of the
              main body of the query. This will not work for
              queries that go beyond simple AND conjunctions of
              comparisons, such as those which use OR. Use
              explicit joins, outerjoins, or
              :meth:`~.RelationshipProperty.Comparator.has` for
              more comprehensive non-many-to-one scalar
              membership tests.
            * Comparisons against ``None`` given in a one-to-many
              or many-to-many context produce a NOT EXISTS clause.

            """
            if isinstance(other, (util.NoneType, expression.Null)):
                if self.property.direction in [ONETOMANY, MANYTOMANY]:
                    return ~self._criterion_exists()
                else:
                    return _orm_annotate(
                        self.property._optimized_compare(
                            None, adapt_source=self.adapter
                        )
                    )
            elif self.property.uselist:
                raise sa_exc.InvalidRequestError(
                    "Can't compare a collection to an object or collection; "
                    "use contains() to test for membership."
                )
            else:
                return _orm_annotate(
                    self.property._optimized_compare(
                        other, adapt_source=self.adapter
                    )
                )

        def _criterion_exists(self, criterion=None, **kwargs):
            if getattr(self, "_of_type", None):
                info = inspect(self._of_type)
                target_mapper, to_selectable, is_aliased_class = (
                    info.mapper,
                    info.selectable,
                    info.is_aliased_class,
                )
                if self.property._is_self_referential and not is_aliased_class:
                    to_selectable = to_selectable._anonymous_fromclause()

                single_crit = target_mapper._single_table_criterion
                if single_crit is not None:
                    if criterion is not None:
                        criterion = single_crit & criterion
                    else:
                        criterion = single_crit
            else:
                is_aliased_class = False
                to_selectable = None

            if self.adapter:
                source_selectable = self._source_selectable()
            else:
                source_selectable = None

            (
                pj,
                sj,
                source,
                dest,
                secondary,
                target_adapter,
            ) = self.property._create_joins(
                dest_selectable=to_selectable,
                source_selectable=source_selectable,
            )

            for k in kwargs:
                crit = getattr(self.property.mapper.class_, k) == kwargs[k]
                if criterion is None:
                    criterion = crit
                else:
                    criterion = criterion & crit

            # annotate the *local* side of the join condition, in the case
            # of pj + sj this is the full primaryjoin, in the case of just
            # pj its the local side of the primaryjoin.
            if sj is not None:
                j = _orm_annotate(pj) & sj
            else:
                j = _orm_annotate(pj, exclude=self.property.remote_side)

            if (
                criterion is not None
                and target_adapter
                and not is_aliased_class
            ):
                # limit this adapter to annotated only?
                criterion = target_adapter.traverse(criterion)

            # only have the "joined left side" of what we
            # return be subject to Query adaption.  The right
            # side of it is used for an exists() subquery and
            # should not correlate or otherwise reach out
            # to anything in the enclosing query.
            if criterion is not None:
                criterion = criterion._annotate(
                    {"no_replacement_traverse": True}
                )

            crit = j & sql.True_._ifnone(criterion)

            if secondary is not None:
                ex = sql.exists(
                    [1], crit, from_obj=[dest, secondary]
                ).correlate_except(dest, secondary)
            else:
                ex = sql.exists([1], crit, from_obj=dest).correlate_except(
                    dest
                )
            return ex

        def any(self, criterion=None, **kwargs):
            """Produce an expression that tests a collection against
            particular criterion, using EXISTS.

            An expression like::

                session.query(MyClass).filter(
                    MyClass.somereference.any(SomeRelated.x==2)
                )


            Will produce a query like::

                SELECT * FROM my_table WHERE
                EXISTS (SELECT 1 FROM related WHERE related.my_id=my_table.id
                AND related.x=2)

            Because :meth:`~.RelationshipProperty.Comparator.any` uses
            a correlated subquery, its performance is not nearly as
            good when compared against large target tables as that of
            using a join.

            :meth:`~.RelationshipProperty.Comparator.any` is particularly
            useful for testing for empty collections::

                session.query(MyClass).filter(
                    ~MyClass.somereference.any()
                )

            will produce::

                SELECT * FROM my_table WHERE
                NOT (EXISTS (SELECT 1 FROM related WHERE
                related.my_id=my_table.id))

            :meth:`~.RelationshipProperty.Comparator.any` is only
            valid for collections, i.e. a :func:`_orm.relationship`
            that has ``uselist=True``.  For scalar references,
            use :meth:`~.RelationshipProperty.Comparator.has`.

            """
            if not self.property.uselist:
                raise sa_exc.InvalidRequestError(
                    "'any()' not implemented for scalar "
                    "attributes. Use has()."
                )

            return self._criterion_exists(criterion, **kwargs)

        def has(self, criterion=None, **kwargs):
            """Produce an expression that tests a scalar reference against
            particular criterion, using EXISTS.

            An expression like::

                session.query(MyClass).filter(
                    MyClass.somereference.has(SomeRelated.x==2)
                )


            Will produce a query like::

                SELECT * FROM my_table WHERE
                EXISTS (SELECT 1 FROM related WHERE
                related.id==my_table.related_id AND related.x=2)

            Because :meth:`~.RelationshipProperty.Comparator.has` uses
            a correlated subquery, its performance is not nearly as
            good when compared against large target tables as that of
            using a join.

            :meth:`~.RelationshipProperty.Comparator.has` is only
            valid for scalar references, i.e. a :func:`_orm.relationship`
            that has ``uselist=False``.  For collection references,
            use :meth:`~.RelationshipProperty.Comparator.any`.

            """
            if self.property.uselist:
                raise sa_exc.InvalidRequestError(
                    "'has()' not implemented for collections.  " "Use any()."
                )
            return self._criterion_exists(criterion, **kwargs)

        def contains(self, other, **kwargs):
            """Return a simple expression that tests a collection for
            containment of a particular item.

            :meth:`~.RelationshipProperty.Comparator.contains` is
            only valid for a collection, i.e. a
            :func:`_orm.relationship` that implements
            one-to-many or many-to-many with ``uselist=True``.

            When used in a simple one-to-many context, an
            expression like::

                MyClass.contains(other)

            Produces a clause like::

                mytable.id == <some id>

            Where ``<some id>`` is the value of the foreign key
            attribute on ``other`` which refers to the primary
            key of its parent object. From this it follows that
            :meth:`~.RelationshipProperty.Comparator.contains` is
            very useful when used with simple one-to-many
            operations.

            For many-to-many operations, the behavior of
            :meth:`~.RelationshipProperty.Comparator.contains`
            has more caveats. The association table will be
            rendered in the statement, producing an "implicit"
            join, that is, includes multiple tables in the FROM
            clause which are equated in the WHERE clause::

                query(MyClass).filter(MyClass.contains(other))

            Produces a query like::

                SELECT * FROM my_table, my_association_table AS
                my_association_table_1 WHERE
                my_table.id = my_association_table_1.parent_id
                AND my_association_table_1.child_id = <some id>

            Where ``<some id>`` would be the primary key of
            ``other``. From the above, it is clear that
            :meth:`~.RelationshipProperty.Comparator.contains`
            will **not** work with many-to-many collections when
            used in queries that move beyond simple AND
            conjunctions, such as multiple
            :meth:`~.RelationshipProperty.Comparator.contains`
            expressions joined by OR. In such cases subqueries or
            explicit "outer joins" will need to be used instead.
            See :meth:`~.RelationshipProperty.Comparator.any` for
            a less-performant alternative using EXISTS, or refer
            to :meth:`_query.Query.outerjoin`
            as well as :ref:`ormtutorial_joins`
            for more details on constructing outer joins.

            """
            if not self.property.uselist:
                raise sa_exc.InvalidRequestError(
                    "'contains' not implemented for scalar "
                    "attributes.  Use =="
                )
            clause = self.property._optimized_compare(
                other, adapt_source=self.adapter
            )

            if self.property.secondaryjoin is not None:
                clause.negation_clause = self.__negated_contains_or_equals(
                    other
                )

            return clause

        def __negated_contains_or_equals(self, other):
            if self.property.direction == MANYTOONE:
                state = attributes.instance_state(other)

                def state_bindparam(local_col, state, remote_col):
                    dict_ = state.dict
                    return sql.bindparam(
                        local_col.key,
                        type_=local_col.type,
                        unique=True,
                        callable_=self.property._get_attr_w_warn_on_none(
                            self.property.mapper, state, dict_, remote_col
                        ),
                    )

                def adapt(col):
                    if self.adapter:
                        return self.adapter(col)
                    else:
                        return col

                if self.property._use_get:
                    return sql.and_(
                        *[
                            sql.or_(
                                adapt(x)
                                != state_bindparam(adapt(x), state, y),
                                adapt(x) == None,
                            )
                            for (x, y) in self.property.local_remote_pairs
                        ]
                    )

            criterion = sql.and_(
                *[
                    x == y
                    for (x, y) in zip(
                        self.property.mapper.primary_key,
                        self.property.mapper.primary_key_from_instance(other),
                    )
                ]
            )

            return ~self._criterion_exists(criterion)

        def __ne__(self, other):
            """Implement the ``!=`` operator.

            In a many-to-one context, such as::

              MyClass.some_prop != <some object>

            This will typically produce a clause such as::

              mytable.related_id != <some id>

            Where ``<some id>`` is the primary key of the
            given object.

            The ``!=`` operator provides partial functionality for non-
            many-to-one comparisons:

            * Comparisons against collections are not supported.
              Use
              :meth:`~.RelationshipProperty.Comparator.contains`
              in conjunction with :func:`_expression.not_`.
            * Compared to a scalar one-to-many, will produce a
              clause that compares the target columns in the parent to
              the given target.
            * Compared to a scalar many-to-many, an alias
              of the association table will be rendered as
              well, forming a natural join that is part of the
              main body of the query. This will not work for
              queries that go beyond simple AND conjunctions of
              comparisons, such as those which use OR. Use
              explicit joins, outerjoins, or
              :meth:`~.RelationshipProperty.Comparator.has` in
              conjunction with :func:`_expression.not_` for
              more comprehensive non-many-to-one scalar
              membership tests.
            * Comparisons against ``None`` given in a one-to-many
              or many-to-many context produce an EXISTS clause.

            """
            if isinstance(other, (util.NoneType, expression.Null)):
                if self.property.direction == MANYTOONE:
                    return _orm_annotate(
                        ~self.property._optimized_compare(
                            None, adapt_source=self.adapter
                        )
                    )

                else:
                    return self._criterion_exists()
            elif self.property.uselist:
                raise sa_exc.InvalidRequestError(
                    "Can't compare a collection"
                    " to an object or collection; use "
                    "contains() to test for membership."
                )
            else:
                return _orm_annotate(self.__negated_contains_or_equals(other))

        @util.memoized_property
        @util.preload_module("sqlalchemy.orm.mapper")
        def property(self):
            mapperlib = util.preloaded.orm_mapper
            if mapperlib.Mapper._new_mappers:
                mapperlib.Mapper._configure_all()
            return self.prop

    def _with_parent(self, instance, alias_secondary=True, from_entity=None):
        assert instance is not None
        adapt_source = None
        if from_entity is not None:
            insp = inspect(from_entity)
            if insp.is_aliased_class:
                adapt_source = insp._adapter.adapt_clause
        return self._optimized_compare(
            instance,
            value_is_parent=True,
            adapt_source=adapt_source,
            alias_secondary=alias_secondary,
        )

    def _optimized_compare(
        self,
        state,
        value_is_parent=False,
        adapt_source=None,
        alias_secondary=True,
    ):
        if state is not None:
            try:
                state = inspect(state)
            except sa_exc.NoInspectionAvailable:
                state = None

            if state is None or not getattr(state, "is_instance", False):
                raise sa_exc.ArgumentError(
                    "Mapped instance expected for relationship "
                    "comparison to object.   Classes, queries and other "
                    "SQL elements are not accepted in this context; for "
                    "comparison with a subquery, "
                    "use %s.has(**criteria)." % self
                )
        reverse_direction = not value_is_parent

        if state is None:
            return self._lazy_none_clause(
                reverse_direction, adapt_source=adapt_source
            )

        if not reverse_direction:
            criterion, bind_to_col = (
                self._lazy_strategy._lazywhere,
                self._lazy_strategy._bind_to_col,
            )
        else:
            criterion, bind_to_col = (
                self._lazy_strategy._rev_lazywhere,
                self._lazy_strategy._rev_bind_to_col,
            )

        if reverse_direction:
            mapper = self.mapper
        else:
            mapper = self.parent

        dict_ = attributes.instance_dict(state.obj())

        def visit_bindparam(bindparam):
            if bindparam._identifying_key in bind_to_col:
                bindparam.callable = self._get_attr_w_warn_on_none(
                    mapper,
                    state,
                    dict_,
                    bind_to_col[bindparam._identifying_key],
                )

        if self.secondary is not None and alias_secondary:
            criterion = ClauseAdapter(
                self.secondary._anonymous_fromclause()
            ).traverse(criterion)

        criterion = visitors.cloned_traverse(
            criterion, {}, {"bindparam": visit_bindparam}
        )

        if adapt_source:
            criterion = adapt_source(criterion)
        return criterion

    def _get_attr_w_warn_on_none(self, mapper, state, dict_, column):
        """Create the callable that is used in a many-to-one expression.

        E.g.::

            u1 = s.query(User).get(5)

            expr = Address.user == u1

        Above, the SQL should be "address.user_id = 5". The callable
        returned by this method produces the value "5" based on the identity
        of ``u1``.

        """

        # in this callable, we're trying to thread the needle through
        # a wide variety of scenarios, including:
        #
        # * the object hasn't been flushed yet and there's no value for
        #   the attribute as of yet
        #
        # * the object hasn't been flushed yet but it has a user-defined
        #   value
        #
        # * the object has a value but it's expired and not locally present
        #
        # * the object has a value but it's expired and not locally present,
        #   and the object is also detached
        #
        # * The object hadn't been flushed yet, there was no value, but
        #   later, the object has been expired and detached, and *now*
        #   they're trying to evaluate it
        #
        # * the object had a value, but it was changed to a new value, and
        #   then expired
        #
        # * the object had a value, but it was changed to a new value, and
        #   then expired, then the object was detached
        #
        # * the object has a user-set value, but it's None and we don't do
        #   the comparison correctly for that so warn
        #

        prop = mapper.get_property_by_column(column)

        # by invoking this method, InstanceState will track the last known
        # value for this key each time the attribute is to be expired.
        # this feature was added explicitly for use in this method.
        state._track_last_known_value(prop.key)

        def _go():
            last_known = to_return = state._last_known_values[prop.key]
            existing_is_available = last_known is not attributes.NO_VALUE

            # we support that the value may have changed.  so here we
            # try to get the most recent value including re-fetching.
            # only if we can't get a value now due to detachment do we return
            # the last known value
            current_value = mapper._get_state_attr_by_column(
                state,
                dict_,
                column,
                passive=attributes.PASSIVE_OFF
                if state.persistent
                else attributes.PASSIVE_NO_FETCH ^ attributes.INIT_OK,
            )

            if current_value is attributes.NEVER_SET:
                if not existing_is_available:
                    raise sa_exc.InvalidRequestError(
                        "Can't resolve value for column %s on object "
                        "%s; no value has been set for this column"
                        % (column, state_str(state))
                    )
            elif current_value is attributes.PASSIVE_NO_RESULT:
                if not existing_is_available:
                    raise sa_exc.InvalidRequestError(
                        "Can't resolve value for column %s on object "
                        "%s; the object is detached and the value was "
                        "expired" % (column, state_str(state))
                    )
            else:
                to_return = current_value
            if to_return is None:
                util.warn(
                    "Got None for value of column %s; this is unsupported "
                    "for a relationship comparison and will not "
                    "currently produce an IS comparison "
                    "(but may in a future release)" % column
                )
            return to_return

        return _go

    def _lazy_none_clause(self, reverse_direction=False, adapt_source=None):
        if not reverse_direction:
            criterion, bind_to_col = (
                self._lazy_strategy._lazywhere,
                self._lazy_strategy._bind_to_col,
            )
        else:
            criterion, bind_to_col = (
                self._lazy_strategy._rev_lazywhere,
                self._lazy_strategy._rev_bind_to_col,
            )

        criterion = adapt_criterion_to_null(criterion, bind_to_col)

        if adapt_source:
            criterion = adapt_source(criterion)
        return criterion

    def __str__(self):
        return str(self.parent.class_.__name__) + "." + self.key

    def merge(
        self,
        session,
        source_state,
        source_dict,
        dest_state,
        dest_dict,
        load,
        _recursive,
        _resolve_conflict_map,
    ):

        if load:
            for r in self._reverse_property:
                if (source_state, r) in _recursive:
                    return

        if "merge" not in self._cascade:
            return

        if self.key not in source_dict:
            return

        if self.uselist:
            impl = source_state.get_impl(self.key)
            instances_iterable = impl.get_collection(source_state, source_dict)

            # if this is a CollectionAttributeImpl, then empty should
            # be False, otherwise "self.key in source_dict" should not be
            # True
            assert not instances_iterable.empty if impl.collection else True

            if load:
                # for a full merge, pre-load the destination collection,
                # so that individual _merge of each item pulls from identity
                # map for those already present.
                # also assumes CollectionAttributeImpl behavior of loading
                # "old" list in any case
                dest_state.get_impl(self.key).get(dest_state, dest_dict)

            dest_list = []
            for current in instances_iterable:
                current_state = attributes.instance_state(current)
                current_dict = attributes.instance_dict(current)
                _recursive[(current_state, self)] = True
                obj = session._merge(
                    current_state,
                    current_dict,
                    load=load,
                    _recursive=_recursive,
                    _resolve_conflict_map=_resolve_conflict_map,
                )
                if obj is not None:
                    dest_list.append(obj)

            if not load:
                coll = attributes.init_state_collection(
                    dest_state, dest_dict, self.key
                )
                for c in dest_list:
                    coll.append_without_event(c)
            else:
                dest_state.get_impl(self.key).set(
                    dest_state, dest_dict, dest_list, _adapt=False
                )
        else:
            current = source_dict[self.key]
            if current is not None:
                current_state = attributes.instance_state(current)
                current_dict = attributes.instance_dict(current)
                _recursive[(current_state, self)] = True
                obj = session._merge(
                    current_state,
                    current_dict,
                    load=load,
                    _recursive=_recursive,
                    _resolve_conflict_map=_resolve_conflict_map,
                )
            else:
                obj = None

            if not load:
                dest_dict[self.key] = obj
            else:
                dest_state.get_impl(self.key).set(
                    dest_state, dest_dict, obj, None
                )

    def _value_as_iterable(
        self, state, dict_, key, passive=attributes.PASSIVE_OFF
    ):
        """Return a list of tuples (state, obj) for the given
        key.

        returns an empty list if the value is None/empty/PASSIVE_NO_RESULT
        """

        impl = state.manager[key].impl
        x = impl.get(state, dict_, passive=passive)
        if x is attributes.PASSIVE_NO_RESULT or x is None:
            return []
        elif hasattr(impl, "get_collection"):
            return [
                (attributes.instance_state(o), o)
                for o in impl.get_collection(state, dict_, x, passive=passive)
            ]
        else:
            return [(attributes.instance_state(x), x)]

    def cascade_iterator(
        self, type_, state, dict_, visited_states, halt_on=None
    ):
        # assert type_ in self._cascade

        # only actively lazy load on the 'delete' cascade
        if type_ != "delete" or self.passive_deletes:
            passive = attributes.PASSIVE_NO_INITIALIZE
        else:
            passive = attributes.PASSIVE_OFF

        if type_ == "save-update":
            tuples = state.manager[self.key].impl.get_all_pending(state, dict_)

        else:
            tuples = self._value_as_iterable(
                state, dict_, self.key, passive=passive
            )

        skip_pending = (
            type_ == "refresh-expire" and "delete-orphan" not in self._cascade
        )

        for instance_state, c in tuples:
            if instance_state in visited_states:
                continue

            if c is None:
                # would like to emit a warning here, but
                # would not be consistent with collection.append(None)
                # current behavior of silently skipping.
                # see [ticket:2229]
                continue

            instance_dict = attributes.instance_dict(c)

            if halt_on and halt_on(instance_state):
                continue

            if skip_pending and not instance_state.key:
                continue

            instance_mapper = instance_state.manager.mapper

            if not instance_mapper.isa(self.mapper.class_manager.mapper):
                raise AssertionError(
                    "Attribute '%s' on class '%s' "
                    "doesn't handle objects "
                    "of type '%s'"
                    % (self.key, self.parent.class_, c.__class__)
                )

            visited_states.add(instance_state)

            yield c, instance_mapper, instance_state, instance_dict

    @property
    def _effective_sync_backref(self):
        return self.sync_backref is not False

    @staticmethod
    def _check_sync_backref(rel_a, rel_b):
        if rel_a.viewonly and rel_b.sync_backref:
            raise sa_exc.InvalidRequestError(
                "Relationship %s cannot specify sync_backref=True since %s "
                "includes viewonly=True." % (rel_b, rel_a)
            )
        if rel_a.viewonly and rel_b.sync_backref is not False:
            util.warn_limited(
                "Setting backref / back_populates on relationship %s to refer "
                "to viewonly relationship %s should include "
                "sync_backref=False set on the %s relationship. ",
                (rel_b, rel_a, rel_b),
            )

    def _add_reverse_property(self, key):
        other = self.mapper.get_property(key, _configure_mappers=False)
        # viewonly and sync_backref cases
        # 1. self.viewonly==True and other.sync_backref==True -> error
        # 2. self.viewonly==True and other.viewonly==False and
        #    other.sync_backref==None -> warn sync_backref=False, set to False
        self._check_sync_backref(self, other)
        # 3. other.viewonly==True and self.sync_backref==True -> error
        # 4. other.viewonly==True and self.viewonly==False and
        #    self.sync_backref==None -> warn sync_backref=False, set to False
        self._check_sync_backref(other, self)

        self._reverse_property.add(other)
        other._reverse_property.add(self)

        if not other.mapper.common_parent(self.parent):
            raise sa_exc.ArgumentError(
                "reverse_property %r on "
                "relationship %s references relationship %s, which "
                "does not reference mapper %s"
                % (key, self, other, self.parent)
            )

        if (
            self.direction in (ONETOMANY, MANYTOONE)
            and self.direction == other.direction
        ):
            raise sa_exc.ArgumentError(
                "%s and back-reference %s are "
                "both of the same direction %r.  Did you mean to "
                "set remote_side on the many-to-one side ?"
                % (other, self, self.direction)
            )

    @util.memoized_property
    @util.preload_module("sqlalchemy.orm.mapper")
    def entity(self):  # type: () -> Union[AliasedInsp, mapperlib.Mapper]
        """Return the target mapped entity, which is an inspect() of the
        class or aliased class that is referred towards.

        """
        mapperlib = util.preloaded.orm_mapper
        if callable(self.argument) and not isinstance(
            self.argument, (type, mapperlib.Mapper)
        ):
            argument = self.argument()
        else:
            argument = self.argument

        if isinstance(argument, type):
            return mapperlib.class_mapper(argument, configure=False)

        try:
            entity = inspect(argument)
        except sa_exc.NoInspectionAvailable:
            pass
        else:
            if hasattr(entity, "mapper"):
                return entity

        raise sa_exc.ArgumentError(
            "relationship '%s' expects "
            "a class or a mapper argument (received: %s)"
            % (self.key, type(argument))
        )

    @util.memoized_property
    def mapper(self):
        """Return the targeted :class:`_orm.Mapper` for this
        :class:`.RelationshipProperty`.

        This is a lazy-initializing static attribute.

        """
        return self.entity.mapper

    def do_init(self):
        self._check_conflicts()
        self._process_dependent_arguments()
        self._setup_join_conditions()
        self._check_cascade_settings(self._cascade)
        self._post_init()
        self._generate_backref()
        self._join_condition._warn_for_conflicting_sync_targets()
        super(RelationshipProperty, self).do_init()
        self._lazy_strategy = self._get_strategy((("lazy", "select"),))

    def _process_dependent_arguments(self):
        """Convert incoming configuration arguments to their
        proper form.

        Callables are resolved, ORM annotations removed.

        """
        # accept callables for other attributes which may require
        # deferred initialization.  This technique is used
        # by declarative "string configs" and some recipes.
        for attr in (
            "order_by",
            "primaryjoin",
            "secondaryjoin",
            "secondary",
            "_user_defined_foreign_keys",
            "remote_side",
        ):
            attr_value = getattr(self, attr)
            if callable(attr_value):
                setattr(self, attr, attr_value())

        # remove "annotations" which are present if mapped class
        # descriptors are used to create the join expression.
        for attr in "primaryjoin", "secondaryjoin":
            val = getattr(self, attr)
            if val is not None:
                setattr(
                    self,
                    attr,
                    _orm_deannotate(
                        coercions.expect(
                            roles.ColumnArgumentRole, val, argname=attr
                        )
                    ),
                )

        # ensure expressions in self.order_by, foreign_keys,
        # remote_side are all columns, not strings.
        if self.order_by is not False and self.order_by is not None:
            self.order_by = [
                coercions.expect(
                    roles.ColumnArgumentRole, x, argname="order_by"
                )
                for x in util.to_list(self.order_by)
            ]

        self._user_defined_foreign_keys = util.column_set(
            coercions.expect(
                roles.ColumnArgumentRole, x, argname="foreign_keys"
            )
            for x in util.to_column_set(self._user_defined_foreign_keys)
        )

        self.remote_side = util.column_set(
            coercions.expect(
                roles.ColumnArgumentRole, x, argname="remote_side"
            )
            for x in util.to_column_set(self.remote_side)
        )

        self.target = self.entity.persist_selectable

    def _setup_join_conditions(self):
        self._join_condition = jc = JoinCondition(
            parent_persist_selectable=self.parent.persist_selectable,
            child_persist_selectable=self.entity.persist_selectable,
            parent_local_selectable=self.parent.local_table,
            child_local_selectable=self.entity.local_table,
            primaryjoin=self.primaryjoin,
            secondary=self.secondary,
            secondaryjoin=self.secondaryjoin,
            parent_equivalents=self.parent._equivalent_columns,
            child_equivalents=self.mapper._equivalent_columns,
            consider_as_foreign_keys=self._user_defined_foreign_keys,
            local_remote_pairs=self.local_remote_pairs,
            remote_side=self.remote_side,
            self_referential=self._is_self_referential,
            prop=self,
            support_sync=not self.viewonly,
            can_be_synced_fn=self._columns_are_mapped,
        )
        self.primaryjoin = jc.primaryjoin
        self.secondaryjoin = jc.secondaryjoin
        self.direction = jc.direction
        self.local_remote_pairs = jc.local_remote_pairs
        self.remote_side = jc.remote_columns
        self.local_columns = jc.local_columns
        self.synchronize_pairs = jc.synchronize_pairs
        self._calculated_foreign_keys = jc.foreign_key_columns
        self.secondary_synchronize_pairs = jc.secondary_synchronize_pairs

    @util.preload_module("sqlalchemy.orm.mapper")
    def _check_conflicts(self):
        """Test that this relationship is legal, warn about
        inheritance conflicts."""
        mapperlib = util.preloaded.orm_mapper
        if self.parent.non_primary and not mapperlib.class_mapper(
            self.parent.class_, configure=False
        ).has_property(self.key):
            raise sa_exc.ArgumentError(
                "Attempting to assign a new "
                "relationship '%s' to a non-primary mapper on "
                "class '%s'.  New relationships can only be added "
                "to the primary mapper, i.e. the very first mapper "
                "created for class '%s' "
                % (
                    self.key,
                    self.parent.class_.__name__,
                    self.parent.class_.__name__,
                )
            )

    @property
    def cascade(self):
        """Return the current cascade setting for this
        :class:`.RelationshipProperty`.
        """
        return self._cascade

    @cascade.setter
    def cascade(self, cascade):
        self._set_cascade(cascade)

    def _set_cascade(self, cascade):
        cascade = CascadeOptions(cascade)

        if self.viewonly:
            non_viewonly = set(cascade).difference(
                CascadeOptions._viewonly_cascades
            )
            if non_viewonly:
                raise sa_exc.ArgumentError(
                    'Cascade settings "%s" apply to persistence operations '
                    "and should not be combined with a viewonly=True "
                    "relationship." % (", ".join(sorted(non_viewonly)))
                )

        if "mapper" in self.__dict__:
            self._check_cascade_settings(cascade)
        self._cascade = cascade

        if self._dependency_processor:
            self._dependency_processor.cascade = cascade

    def _check_cascade_settings(self, cascade):
        if (
            cascade.delete_orphan
            and not self.single_parent
            and (self.direction is MANYTOMANY or self.direction is MANYTOONE)
        ):
            raise sa_exc.ArgumentError(
                "For %(direction)s relationship %(rel)s, delete-orphan "
                "cascade is normally "
                'configured only on the "one" side of a one-to-many '
                "relationship, "
                'and not on the "many" side of a many-to-one or many-to-many '
                "relationship.  "
                "To force this relationship to allow a particular "
                '"%(relatedcls)s" object to be referred towards by only '
                'a single "%(clsname)s" object at a time via the '
                "%(rel)s relationship, which "
                "would allow "
                "delete-orphan cascade to take place in this direction, set "
                "the single_parent=True flag."
                % {
                    "rel": self,
                    "direction": "many-to-one"
                    if self.direction is MANYTOONE
                    else "many-to-many",
                    "clsname": self.parent.class_.__name__,
                    "relatedcls": self.mapper.class_.__name__,
                },
                code="bbf0",
            )

        if self.direction is MANYTOONE and self.passive_deletes:
            util.warn(
                "On %s, 'passive_deletes' is normally configured "
                "on one-to-many, one-to-one, many-to-many "
                "relationships only." % self
            )

        if self.passive_deletes == "all" and (
            "delete" in cascade or "delete-orphan" in cascade
        ):
            raise sa_exc.ArgumentError(
                "On %s, can't set passive_deletes='all' in conjunction "
                "with 'delete' or 'delete-orphan' cascade" % self
            )

        if cascade.delete_orphan:
            self.mapper.primary_mapper()._delete_orphans.append(
                (self.key, self.parent.class_)
            )

    def _persists_for(self, mapper):
        """Return True if this property will persist values on behalf
        of the given mapper.

        """

        return (
            self.key in mapper.relationships
            and mapper.relationships[self.key] is self
        )

    def _columns_are_mapped(self, *cols):
        """Return True if all columns in the given collection are
        mapped by the tables referenced by this :class:`.Relationship`.

        """
        for c in cols:
            if (
                self.secondary is not None
                and self.secondary.c.contains_column(c)
            ):
                continue
            if not self.parent.persist_selectable.c.contains_column(
                c
            ) and not self.target.c.contains_column(c):
                return False
        return True

    def _generate_backref(self):
        """Interpret the 'backref' instruction to create a
        :func:`_orm.relationship` complementary to this one."""

        if self.parent.non_primary:
            return
        if self.backref is not None and not self.back_populates:
            if isinstance(self.backref, util.string_types):
                backref_key, kwargs = self.backref, {}
            else:
                backref_key, kwargs = self.backref
            mapper = self.mapper.primary_mapper()

            if not mapper.concrete:
                check = set(mapper.iterate_to_root()).union(
                    mapper.self_and_descendants
                )
                for m in check:
                    if m.has_property(backref_key) and not m.concrete:
                        raise sa_exc.ArgumentError(
                            "Error creating backref "
                            "'%s' on relationship '%s': property of that "
                            "name exists on mapper '%s'"
                            % (backref_key, self, m)
                        )

            # determine primaryjoin/secondaryjoin for the
            # backref.  Use the one we had, so that
            # a custom join doesn't have to be specified in
            # both directions.
            if self.secondary is not None:
                # for many to many, just switch primaryjoin/
                # secondaryjoin.   use the annotated
                # pj/sj on the _join_condition.
                pj = kwargs.pop(
                    "primaryjoin",
                    self._join_condition.secondaryjoin_minus_local,
                )
                sj = kwargs.pop(
                    "secondaryjoin",
                    self._join_condition.primaryjoin_minus_local,
                )
            else:
                pj = kwargs.pop(
                    "primaryjoin",
                    self._join_condition.primaryjoin_reverse_remote,
                )
                sj = kwargs.pop("secondaryjoin", None)
                if sj:
                    raise sa_exc.InvalidRequestError(
                        "Can't assign 'secondaryjoin' on a backref "
                        "against a non-secondary relationship."
                    )

            foreign_keys = kwargs.pop(
                "foreign_keys", self._user_defined_foreign_keys
            )
            parent = self.parent.primary_mapper()
            kwargs.setdefault("viewonly", self.viewonly)
            kwargs.setdefault("post_update", self.post_update)
            kwargs.setdefault("passive_updates", self.passive_updates)
            kwargs.setdefault("sync_backref", self.sync_backref)
            self.back_populates = backref_key
            relationship = RelationshipProperty(
                parent,
                self.secondary,
                pj,
                sj,
                foreign_keys=foreign_keys,
                back_populates=self.key,
                **kwargs
            )
            mapper._configure_property(backref_key, relationship)

        if self.back_populates:
            self._add_reverse_property(self.back_populates)

    @util.preload_module("sqlalchemy.orm.dependency")
    def _post_init(self):
        dependency = util.preloaded.orm_dependency

        if self.uselist is None:
            self.uselist = self.direction is not MANYTOONE
        if not self.viewonly:
            self._dependency_processor = (
                dependency.DependencyProcessor.from_relationship
            )(self)

    @util.memoized_property
    def _use_get(self):
        """memoize the 'use_get' attribute of this RelationshipLoader's
        lazyloader."""

        strategy = self._lazy_strategy
        return strategy.use_get

    @util.memoized_property
    def _is_self_referential(self):
        return self.mapper.common_parent(self.parent)

    def _create_joins(
        self,
        source_polymorphic=False,
        source_selectable=None,
        dest_selectable=None,
        of_type_entity=None,
        alias_secondary=False,
    ):

        aliased = False

        if alias_secondary and self.secondary is not None:
            aliased = True

        if source_selectable is None:
            if source_polymorphic and self.parent.with_polymorphic:
                source_selectable = self.parent._with_polymorphic_selectable

        if of_type_entity:
            dest_mapper = of_type_entity.mapper
            if dest_selectable is None:
                dest_selectable = of_type_entity.selectable
                aliased = True
        else:
            dest_mapper = self.mapper

        if dest_selectable is None:
            dest_selectable = self.entity.selectable
            if self.mapper.with_polymorphic:
                aliased = True

            if self._is_self_referential and source_selectable is None:
                dest_selectable = dest_selectable._anonymous_fromclause()
                aliased = True
        elif (
            dest_selectable is not self.mapper._with_polymorphic_selectable
            or self.mapper.with_polymorphic
        ):
            aliased = True

        single_crit = dest_mapper._single_table_criterion
        aliased = aliased or (
            source_selectable is not None
            and (
                source_selectable
                is not self.parent._with_polymorphic_selectable
                or source_selectable._is_subquery
            )
        )

        (
            primaryjoin,
            secondaryjoin,
            secondary,
            target_adapter,
            dest_selectable,
        ) = self._join_condition.join_targets(
            source_selectable, dest_selectable, aliased, single_crit
        )
        if source_selectable is None:
            source_selectable = self.parent.local_table
        if dest_selectable is None:
            dest_selectable = self.entity.local_table
        return (
            primaryjoin,
            secondaryjoin,
            source_selectable,
            dest_selectable,
            secondary,
            target_adapter,
        )


def _annotate_columns(element, annotations):
    def clone(elem):
        if isinstance(elem, expression.ColumnClause):
            elem = elem._annotate(annotations.copy())
        elem._copy_internals(clone=clone)
        return elem

    if element is not None:
        element = clone(element)
    clone = None  # remove gc cycles
    return element


class JoinCondition(object):
    def __init__(
        self,
        parent_persist_selectable,
        child_persist_selectable,
        parent_local_selectable,
        child_local_selectable,
        primaryjoin=None,
        secondary=None,
        secondaryjoin=None,
        parent_equivalents=None,
        child_equivalents=None,
        consider_as_foreign_keys=None,
        local_remote_pairs=None,
        remote_side=None,
        self_referential=False,
        prop=None,
        support_sync=True,
        can_be_synced_fn=lambda *c: True,
    ):
        self.parent_persist_selectable = parent_persist_selectable
        self.parent_local_selectable = parent_local_selectable
        self.child_persist_selectable = child_persist_selectable
        self.child_local_selectable = child_local_selectable
        self.parent_equivalents = parent_equivalents
        self.child_equivalents = child_equivalents
        self.primaryjoin = primaryjoin
        self.secondaryjoin = secondaryjoin
        self.secondary = secondary
        self.consider_as_foreign_keys = consider_as_foreign_keys
        self._local_remote_pairs = local_remote_pairs
        self._remote_side = remote_side
        self.prop = prop
        self.self_referential = self_referential
        self.support_sync = support_sync
        self.can_be_synced_fn = can_be_synced_fn
        self._determine_joins()
        self._sanitize_joins()
        self._annotate_fks()
        self._annotate_remote()
        self._annotate_local()
        self._annotate_parentmapper()
        self._setup_pairs()
        self._check_foreign_cols(self.primaryjoin, True)
        if self.secondaryjoin is not None:
            self._check_foreign_cols(self.secondaryjoin, False)
        self._determine_direction()
        self._check_remote_side()
        self._log_joins()

    def _log_joins(self):
        if self.prop is None:
            return
        log = self.prop.logger
        log.info("%s setup primary join %s", self.prop, self.primaryjoin)
        log.info("%s setup secondary join %s", self.prop, self.secondaryjoin)
        log.info(
            "%s synchronize pairs [%s]",
            self.prop,
            ",".join(
                "(%s => %s)" % (l, r) for (l, r) in self.synchronize_pairs
            ),
        )
        log.info(
            "%s secondary synchronize pairs [%s]",
            self.prop,
            ",".join(
                "(%s => %s)" % (l, r)
                for (l, r) in self.secondary_synchronize_pairs or []
            ),
        )
        log.info(
            "%s local/remote pairs [%s]",
            self.prop,
            ",".join(
                "(%s / %s)" % (l, r) for (l, r) in self.local_remote_pairs
            ),
        )
        log.info(
            "%s remote columns [%s]",
            self.prop,
            ",".join("%s" % col for col in self.remote_columns),
        )
        log.info(
            "%s local columns [%s]",
            self.prop,
            ",".join("%s" % col for col in self.local_columns),
        )
        log.info("%s relationship direction %s", self.prop, self.direction)

    def _sanitize_joins(self):
        """remove the parententity annotation from our join conditions which
        can leak in here based on some declarative patterns and maybe others.

        We'd want to remove "parentmapper" also, but apparently there's
        an exotic use case in _join_fixture_inh_selfref_w_entity
        that relies upon it being present, see :ticket:`3364`.

        """

        self.primaryjoin = _deep_deannotate(
            self.primaryjoin, values=("parententity", "orm_key")
        )
        if self.secondaryjoin is not None:
            self.secondaryjoin = _deep_deannotate(
                self.secondaryjoin, values=("parententity", "orm_key")
            )

    def _determine_joins(self):
        """Determine the 'primaryjoin' and 'secondaryjoin' attributes,
        if not passed to the constructor already.

        This is based on analysis of the foreign key relationships
        between the parent and target mapped selectables.

        """
        if self.secondaryjoin is not None and self.secondary is None:
            raise sa_exc.ArgumentError(
                "Property %s specified with secondary "
                "join condition but "
                "no secondary argument" % self.prop
            )

        # find a join between the given mapper's mapped table and
        # the given table. will try the mapper's local table first
        # for more specificity, then if not found will try the more
        # general mapped table, which in the case of inheritance is
        # a join.
        try:
            consider_as_foreign_keys = self.consider_as_foreign_keys or None
            if self.secondary is not None:
                if self.secondaryjoin is None:
                    self.secondaryjoin = join_condition(
                        self.child_persist_selectable,
                        self.secondary,
                        a_subset=self.child_local_selectable,
                        consider_as_foreign_keys=consider_as_foreign_keys,
                    )
                if self.primaryjoin is None:
                    self.primaryjoin = join_condition(
                        self.parent_persist_selectable,
                        self.secondary,
                        a_subset=self.parent_local_selectable,
                        consider_as_foreign_keys=consider_as_foreign_keys,
                    )
            else:
                if self.primaryjoin is None:
                    self.primaryjoin = join_condition(
                        self.parent_persist_selectable,
                        self.child_persist_selectable,
                        a_subset=self.parent_local_selectable,
                        consider_as_foreign_keys=consider_as_foreign_keys,
                    )
        except sa_exc.NoForeignKeysError as nfe:
            if self.secondary is not None:
                util.raise_(
                    sa_exc.NoForeignKeysError(
                        "Could not determine join "
                        "condition between parent/child tables on "
                        "relationship %s - there are no foreign keys "
                        "linking these tables via secondary table '%s'.  "
                        "Ensure that referencing columns are associated "
                        "with a ForeignKey or ForeignKeyConstraint, or "
                        "specify 'primaryjoin' and 'secondaryjoin' "
                        "expressions." % (self.prop, self.secondary)
                    ),
                    from_=nfe,
                )
            else:
                util.raise_(
                    sa_exc.NoForeignKeysError(
                        "Could not determine join "
                        "condition between parent/child tables on "
                        "relationship %s - there are no foreign keys "
                        "linking these tables.  "
                        "Ensure that referencing columns are associated "
                        "with a ForeignKey or ForeignKeyConstraint, or "
                        "specify a 'primaryjoin' expression." % self.prop
                    ),
                    from_=nfe,
                )
        except sa_exc.AmbiguousForeignKeysError as afe:
            if self.secondary is not None:
                util.raise_(
                    sa_exc.AmbiguousForeignKeysError(
                        "Could not determine join "
                        "condition between parent/child tables on "
                        "relationship %s - there are multiple foreign key "
                        "paths linking the tables via secondary table '%s'.  "
                        "Specify the 'foreign_keys' "
                        "argument, providing a list of those columns which "
                        "should be counted as containing a foreign key "
                        "reference from the secondary table to each of the "
                        "parent and child tables."
                        % (self.prop, self.secondary)
                    ),
                    from_=afe,
                )
            else:
                util.raise_(
                    sa_exc.AmbiguousForeignKeysError(
                        "Could not determine join "
                        "condition between parent/child tables on "
                        "relationship %s - there are multiple foreign key "
                        "paths linking the tables.  Specify the "
                        "'foreign_keys' argument, providing a list of those "
                        "columns which should be counted as containing a "
                        "foreign key reference to the parent table."
                        % self.prop
                    ),
                    from_=afe,
                )

    @property
    def primaryjoin_minus_local(self):
        return _deep_deannotate(self.primaryjoin, values=("local", "remote"))

    @property
    def secondaryjoin_minus_local(self):
        return _deep_deannotate(self.secondaryjoin, values=("local", "remote"))

    @util.memoized_property
    def primaryjoin_reverse_remote(self):
        """Return the primaryjoin condition suitable for the
        "reverse" direction.

        If the primaryjoin was delivered here with pre-existing
        "remote" annotations, the local/remote annotations
        are reversed.  Otherwise, the local/remote annotations
        are removed.

        """
        if self._has_remote_annotations:

            def replace(element):
                if "remote" in element._annotations:
                    v = dict(element._annotations)
                    del v["remote"]
                    v["local"] = True
                    return element._with_annotations(v)
                elif "local" in element._annotations:
                    v = dict(element._annotations)
                    del v["local"]
                    v["remote"] = True
                    return element._with_annotations(v)

            return visitors.replacement_traverse(self.primaryjoin, {}, replace)
        else:
            if self._has_foreign_annotations:
                # TODO: coverage
                return _deep_deannotate(
                    self.primaryjoin, values=("local", "remote")
                )
            else:
                return _deep_deannotate(self.primaryjoin)

    def _has_annotation(self, clause, annotation):
        for col in visitors.iterate(clause, {}):
            if annotation in col._annotations:
                return True
        else:
            return False

    @util.memoized_property
    def _has_foreign_annotations(self):
        return self._has_annotation(self.primaryjoin, "foreign")

    @util.memoized_property
    def _has_remote_annotations(self):
        return self._has_annotation(self.primaryjoin, "remote")

    def _annotate_fks(self):
        """Annotate the primaryjoin and secondaryjoin
        structures with 'foreign' annotations marking columns
        considered as foreign.

        """
        if self._has_foreign_annotations:
            return

        if self.consider_as_foreign_keys:
            self._annotate_from_fk_list()
        else:
            self._annotate_present_fks()

    def _annotate_from_fk_list(self):
        def check_fk(col):
            if col in self.consider_as_foreign_keys:
                return col._annotate({"foreign": True})

        self.primaryjoin = visitors.replacement_traverse(
            self.primaryjoin, {}, check_fk
        )
        if self.secondaryjoin is not None:
            self.secondaryjoin = visitors.replacement_traverse(
                self.secondaryjoin, {}, check_fk
            )

    def _annotate_present_fks(self):
        if self.secondary is not None:
            secondarycols = util.column_set(self.secondary.c)
        else:
            secondarycols = set()

        def is_foreign(a, b):
            if isinstance(a, schema.Column) and isinstance(b, schema.Column):
                if a.references(b):
                    return a
                elif b.references(a):
                    return b

            if secondarycols:
                if a in secondarycols and b not in secondarycols:
                    return a
                elif b in secondarycols and a not in secondarycols:
                    return b

        def visit_binary(binary):
            if not isinstance(
                binary.left, sql.ColumnElement
            ) or not isinstance(binary.right, sql.ColumnElement):
                return

            if (
                "foreign" not in binary.left._annotations
                and "foreign" not in binary.right._annotations
            ):
                col = is_foreign(binary.left, binary.right)
                if col is not None:
                    if col.compare(binary.left):
                        binary.left = binary.left._annotate({"foreign": True})
                    elif col.compare(binary.right):
                        binary.right = binary.right._annotate(
                            {"foreign": True}
                        )

        self.primaryjoin = visitors.cloned_traverse(
            self.primaryjoin, {}, {"binary": visit_binary}
        )
        if self.secondaryjoin is not None:
            self.secondaryjoin = visitors.cloned_traverse(
                self.secondaryjoin, {}, {"binary": visit_binary}
            )

    def _refers_to_parent_table(self):
        """Return True if the join condition contains column
        comparisons where both columns are in both tables.

        """
        pt = self.parent_persist_selectable
        mt = self.child_persist_selectable
        result = [False]

        def visit_binary(binary):
            c, f = binary.left, binary.right
            if (
                isinstance(c, expression.ColumnClause)
                and isinstance(f, expression.ColumnClause)
                and pt.is_derived_from(c.table)
                and pt.is_derived_from(f.table)
                and mt.is_derived_from(c.table)
                and mt.is_derived_from(f.table)
            ):
                result[0] = True

        visitors.traverse(self.primaryjoin, {}, {"binary": visit_binary})
        return result[0]

    def _tables_overlap(self):
        """Return True if parent/child tables have some overlap."""

        return selectables_overlap(
            self.parent_persist_selectable, self.child_persist_selectable
        )

    def _annotate_remote(self):
        """Annotate the primaryjoin and secondaryjoin
        structures with 'remote' annotations marking columns
        considered as part of the 'remote' side.

        """
        if self._has_remote_annotations:
            return

        if self.secondary is not None:
            self._annotate_remote_secondary()
        elif self._local_remote_pairs or self._remote_side:
            self._annotate_remote_from_args()
        elif self._refers_to_parent_table():
            self._annotate_selfref(
                lambda col: "foreign" in col._annotations, False
            )
        elif self._tables_overlap():
            self._annotate_remote_with_overlap()
        else:
            self._annotate_remote_distinct_selectables()

    def _annotate_remote_secondary(self):
        """annotate 'remote' in primaryjoin, secondaryjoin
        when 'secondary' is present.

        """

        def repl(element):
            if self.secondary.c.contains_column(element):
                return element._annotate({"remote": True})

        self.primaryjoin = visitors.replacement_traverse(
            self.primaryjoin, {}, repl
        )
        self.secondaryjoin = visitors.replacement_traverse(
            self.secondaryjoin, {}, repl
        )

    def _annotate_selfref(self, fn, remote_side_given):
        """annotate 'remote' in primaryjoin, secondaryjoin
        when the relationship is detected as self-referential.

        """

        def visit_binary(binary):
            equated = binary.left.compare(binary.right)
            if isinstance(binary.left, expression.ColumnClause) and isinstance(
                binary.right, expression.ColumnClause
            ):
                # assume one to many - FKs are "remote"
                if fn(binary.left):
                    binary.left = binary.left._annotate({"remote": True})
                if fn(binary.right) and not equated:
                    binary.right = binary.right._annotate({"remote": True})
            elif not remote_side_given:
                self._warn_non_column_elements()

        self.primaryjoin = visitors.cloned_traverse(
            self.primaryjoin, {}, {"binary": visit_binary}
        )

    def _annotate_remote_from_args(self):
        """annotate 'remote' in primaryjoin, secondaryjoin
        when the 'remote_side' or '_local_remote_pairs'
        arguments are used.

        """
        if self._local_remote_pairs:
            if self._remote_side:
                raise sa_exc.ArgumentError(
                    "remote_side argument is redundant "
                    "against more detailed _local_remote_side "
                    "argument."
                )

            remote_side = [r for (l, r) in self._local_remote_pairs]
        else:
            remote_side = self._remote_side

        if self._refers_to_parent_table():
            self._annotate_selfref(lambda col: col in remote_side, True)
        else:

            def repl(element):
                # use set() to avoid generating ``__eq__()`` expressions
                # against each element
                if element in set(remote_side):
                    return element._annotate({"remote": True})

            self.primaryjoin = visitors.replacement_traverse(
                self.primaryjoin, {}, repl
            )

    def _annotate_remote_with_overlap(self):
        """annotate 'remote' in primaryjoin, secondaryjoin
        when the parent/child tables have some set of
        tables in common, though is not a fully self-referential
        relationship.

        """

        def visit_binary(binary):
            binary.left, binary.right = proc_left_right(
                binary.left, binary.right
            )
            binary.right, binary.left = proc_left_right(
                binary.right, binary.left
            )

        check_entities = (
            self.prop is not None and self.prop.mapper is not self.prop.parent
        )

        def proc_left_right(left, right):
            if isinstance(left, expression.ColumnClause) and isinstance(
                right, expression.ColumnClause
            ):
                if self.child_persist_selectable.c.contains_column(
                    right
                ) and self.parent_persist_selectable.c.contains_column(left):
                    right = right._annotate({"remote": True})
            elif (
                check_entities
                and right._annotations.get("parentmapper") is self.prop.mapper
            ):
                right = right._annotate({"remote": True})
            elif (
                check_entities
                and left._annotations.get("parentmapper") is self.prop.mapper
            ):
                left = left._annotate({"remote": True})
            else:
                self._warn_non_column_elements()

            return left, right

        self.primaryjoin = visitors.cloned_traverse(
            self.primaryjoin, {}, {"binary": visit_binary}
        )

    def _annotate_remote_distinct_selectables(self):
        """annotate 'remote' in primaryjoin, secondaryjoin
        when the parent/child tables are entirely
        separate.

        """

        def repl(element):
            if self.child_persist_selectable.c.contains_column(element) and (
                not self.parent_local_selectable.c.contains_column(element)
                or self.child_local_selectable.c.contains_column(element)
            ):
                return element._annotate({"remote": True})

        self.primaryjoin = visitors.replacement_traverse(
            self.primaryjoin, {}, repl
        )

    def _warn_non_column_elements(self):
        util.warn(
            "Non-simple column elements in primary "
            "join condition for property %s - consider using "
            "remote() annotations to mark the remote side." % self.prop
        )

    def _annotate_local(self):
        """Annotate the primaryjoin and secondaryjoin
        structures with 'local' annotations.

        This annotates all column elements found
        simultaneously in the parent table
        and the join condition that don't have a
        'remote' annotation set up from
        _annotate_remote() or user-defined.

        """
        if self._has_annotation(self.primaryjoin, "local"):
            return

        if self._local_remote_pairs:
            local_side = util.column_set(
                [l for (l, r) in self._local_remote_pairs]
            )
        else:
            local_side = util.column_set(self.parent_persist_selectable.c)

        def locals_(elem):
            if "remote" not in elem._annotations and elem in local_side:
                return elem._annotate({"local": True})

        self.primaryjoin = visitors.replacement_traverse(
            self.primaryjoin, {}, locals_
        )

    def _annotate_parentmapper(self):
        if self.prop is None:
            return

        def parentmappers_(elem):
            if "remote" in elem._annotations:
                return elem._annotate({"parentmapper": self.prop.mapper})
            elif "local" in elem._annotations:
                return elem._annotate({"parentmapper": self.prop.parent})

        self.primaryjoin = visitors.replacement_traverse(
            self.primaryjoin, {}, parentmappers_
        )

    def _check_remote_side(self):
        if not self.local_remote_pairs:
            raise sa_exc.ArgumentError(
                "Relationship %s could "
                "not determine any unambiguous local/remote column "
                "pairs based on join condition and remote_side "
                "arguments.  "
                "Consider using the remote() annotation to "
                "accurately mark those elements of the join "
                "condition that are on the remote side of "
                "the relationship." % (self.prop,)
            )

    def _check_foreign_cols(self, join_condition, primary):
        """Check the foreign key columns collected and emit error
        messages."""

        can_sync = False

        foreign_cols = self._gather_columns_with_annotation(
            join_condition, "foreign"
        )

        has_foreign = bool(foreign_cols)

        if primary:
            can_sync = bool(self.synchronize_pairs)
        else:
            can_sync = bool(self.secondary_synchronize_pairs)

        if (
            self.support_sync
            and can_sync
            or (not self.support_sync and has_foreign)
        ):
            return

        # from here below is just determining the best error message
        # to report.  Check for a join condition using any operator
        # (not just ==), perhaps they need to turn on "viewonly=True".
        if self.support_sync and has_foreign and not can_sync:
            err = (
                "Could not locate any simple equality expressions "
                "involving locally mapped foreign key columns for "
                "%s join condition "
                "'%s' on relationship %s."
                % (
                    primary and "primary" or "secondary",
                    join_condition,
                    self.prop,
                )
            )
            err += (
                "  Ensure that referencing columns are associated "
                "with a ForeignKey or ForeignKeyConstraint, or are "
                "annotated in the join condition with the foreign() "
                "annotation. To allow comparison operators other than "
                "'==', the relationship can be marked as viewonly=True."
            )

            raise sa_exc.ArgumentError(err)
        else:
            err = (
                "Could not locate any relevant foreign key columns "
                "for %s join condition '%s' on relationship %s."
                % (
                    primary and "primary" or "secondary",
                    join_condition,
                    self.prop,
                )
            )
            err += (
                "  Ensure that referencing columns are associated "
                "with a ForeignKey or ForeignKeyConstraint, or are "
                "annotated in the join condition with the foreign() "
                "annotation."
            )
            raise sa_exc.ArgumentError(err)

    def _determine_direction(self):
        """Determine if this relationship is one to many, many to one,
        many to many.

        """
        if self.secondaryjoin is not None:
            self.direction = MANYTOMANY
        else:
            parentcols = util.column_set(self.parent_persist_selectable.c)
            targetcols = util.column_set(self.child_persist_selectable.c)

            # fk collection which suggests ONETOMANY.
            onetomany_fk = targetcols.intersection(self.foreign_key_columns)

            # fk collection which suggests MANYTOONE.

            manytoone_fk = parentcols.intersection(self.foreign_key_columns)

            if onetomany_fk and manytoone_fk:
                # fks on both sides.  test for overlap of local/remote
                # with foreign key.
                # we will gather columns directly from their annotations
                # without deannotating, so that we can distinguish on a column
                # that refers to itself.

                # 1. columns that are both remote and FK suggest
                # onetomany.
                onetomany_local = self._gather_columns_with_annotation(
                    self.primaryjoin, "remote", "foreign"
                )

                # 2. columns that are FK but are not remote (e.g. local)
                # suggest manytoone.
                manytoone_local = set(
                    [
                        c
                        for c in self._gather_columns_with_annotation(
                            self.primaryjoin, "foreign"
                        )
                        if "remote" not in c._annotations
                    ]
                )

                # 3. if both collections are present, remove columns that
                # refer to themselves.  This is for the case of
                # and_(Me.id == Me.remote_id, Me.version == Me.version)
                if onetomany_local and manytoone_local:
                    self_equated = self.remote_columns.intersection(
                        self.local_columns
                    )
                    onetomany_local = onetomany_local.difference(self_equated)
                    manytoone_local = manytoone_local.difference(self_equated)

                # at this point, if only one or the other collection is
                # present, we know the direction, otherwise it's still
                # ambiguous.

                if onetomany_local and not manytoone_local:
                    self.direction = ONETOMANY
                elif manytoone_local and not onetomany_local:
                    self.direction = MANYTOONE
                else:
                    raise sa_exc.ArgumentError(
                        "Can't determine relationship"
                        " direction for relationship '%s' - foreign "
                        "key columns within the join condition are present "
                        "in both the parent and the child's mapped tables.  "
                        "Ensure that only those columns referring "
                        "to a parent column are marked as foreign, "
                        "either via the foreign() annotation or "
                        "via the foreign_keys argument." % self.prop
                    )
            elif onetomany_fk:
                self.direction = ONETOMANY
            elif manytoone_fk:
                self.direction = MANYTOONE
            else:
                raise sa_exc.ArgumentError(
                    "Can't determine relationship "
                    "direction for relationship '%s' - foreign "
                    "key columns are present in neither the parent "
                    "nor the child's mapped tables" % self.prop
                )

    def _deannotate_pairs(self, collection):
        """provide deannotation for the various lists of
        pairs, so that using them in hashes doesn't incur
        high-overhead __eq__() comparisons against
        original columns mapped.

        """
        return [(x._deannotate(), y._deannotate()) for x, y in collection]

    def _setup_pairs(self):
        sync_pairs = []
        lrp = util.OrderedSet([])
        secondary_sync_pairs = []

        def go(joincond, collection):
            def visit_binary(binary, left, right):
                if (
                    "remote" in right._annotations
                    and "remote" not in left._annotations
                    and self.can_be_synced_fn(left)
                ):
                    lrp.add((left, right))
                elif (
                    "remote" in left._annotations
                    and "remote" not in right._annotations
                    and self.can_be_synced_fn(right)
                ):
                    lrp.add((right, left))
                if binary.operator is operators.eq and self.can_be_synced_fn(
                    left, right
                ):
                    if "foreign" in right._annotations:
                        collection.append((left, right))
                    elif "foreign" in left._annotations:
                        collection.append((right, left))

            visit_binary_product(visit_binary, joincond)

        for joincond, collection in [
            (self.primaryjoin, sync_pairs),
            (self.secondaryjoin, secondary_sync_pairs),
        ]:
            if joincond is None:
                continue
            go(joincond, collection)

        self.local_remote_pairs = self._deannotate_pairs(lrp)
        self.synchronize_pairs = self._deannotate_pairs(sync_pairs)
        self.secondary_synchronize_pairs = self._deannotate_pairs(
            secondary_sync_pairs
        )

    _track_overlapping_sync_targets = weakref.WeakKeyDictionary()

    @util.preload_module("sqlalchemy.orm.mapper")
    def _warn_for_conflicting_sync_targets(self):
        mapperlib = util.preloaded.orm_mapper
        if not self.support_sync:
            return

        # we would like to detect if we are synchronizing any column
        # pairs in conflict with another relationship that wishes to sync
        # an entirely different column to the same target.   This is a
        # very rare edge case so we will try to minimize the memory/overhead
        # impact of this check
        for from_, to_ in [
            (from_, to_) for (from_, to_) in self.synchronize_pairs
        ] + [
            (from_, to_) for (from_, to_) in self.secondary_synchronize_pairs
        ]:
            # save ourselves a ton of memory and overhead by only
            # considering columns that are subject to a overlapping
            # FK constraints at the core level.   This condition can arise
            # if multiple relationships overlap foreign() directly, but
            # we're going to assume it's typically a ForeignKeyConstraint-
            # level configuration that benefits from this warning.

            if to_ not in self._track_overlapping_sync_targets:
                self._track_overlapping_sync_targets[
                    to_
                ] = weakref.WeakKeyDictionary({self.prop: from_})
            else:
                other_props = []
                prop_to_from = self._track_overlapping_sync_targets[to_]

                for pr, fr_ in prop_to_from.items():
                    if (
                        pr.mapper in mapperlib._mapper_registry
                        and pr not in self.prop._reverse_property
                        and pr.key not in self.prop._overlaps
                        and self.prop.key not in pr._overlaps
                        and not self.prop.parent.is_sibling(pr.parent)
                        and not self.prop.mapper.is_sibling(pr.mapper)
                        and (
                            self.prop.key != pr.key
                            or not self.prop.parent.common_parent(pr.parent)
                        )
                    ):

                        other_props.append((pr, fr_))

                if other_props:
                    util.warn(
                        "relationship '%s' will copy column %s to column %s, "
                        "which conflicts with relationship(s): %s. "
                        "If this is not the intention, consider if these "
                        "relationships should be linked with "
                        "back_populates, or if viewonly=True should be "
                        "applied to one or more if they are read-only. "
                        "For the less common case that foreign key "
                        "constraints are partially overlapping, the "
                        "orm.foreign() "
                        "annotation can be used to isolate the columns that "
                        "should be written towards.   The 'overlaps' "
                        "parameter may be used to remove this warning."
                        % (
                            self.prop,
                            from_,
                            to_,
                            ", ".join(
                                "'%s' (copies %s to %s)" % (pr, fr_, to_)
                                for (pr, fr_) in other_props
                            ),
                        )
                    )
                self._track_overlapping_sync_targets[to_][self.prop] = from_

    @util.memoized_property
    def remote_columns(self):
        return self._gather_join_annotations("remote")

    @util.memoized_property
    def local_columns(self):
        return self._gather_join_annotations("local")

    @util.memoized_property
    def foreign_key_columns(self):
        return self._gather_join_annotations("foreign")

    def _gather_join_annotations(self, annotation):
        s = set(
            self._gather_columns_with_annotation(self.primaryjoin, annotation)
        )
        if self.secondaryjoin is not None:
            s.update(
                self._gather_columns_with_annotation(
                    self.secondaryjoin, annotation
                )
            )
        return {x._deannotate() for x in s}

    def _gather_columns_with_annotation(self, clause, *annotation):
        annotation = set(annotation)
        return set(
            [
                col
                for col in visitors.iterate(clause, {})
                if annotation.issubset(col._annotations)
            ]
        )

    def join_targets(
        self, source_selectable, dest_selectable, aliased, single_crit=None
    ):
        """Given a source and destination selectable, create a
        join between them.

        This takes into account aliasing the join clause
        to reference the appropriate corresponding columns
        in the target objects, as well as the extra child
        criterion, equivalent column sets, etc.

        """
        # place a barrier on the destination such that
        # replacement traversals won't ever dig into it.
        # its internal structure remains fixed
        # regardless of context.
        dest_selectable = _shallow_annotate(
            dest_selectable, {"no_replacement_traverse": True}
        )

        primaryjoin, secondaryjoin, secondary = (
            self.primaryjoin,
            self.secondaryjoin,
            self.secondary,
        )

        # adjust the join condition for single table inheritance,
        # in the case that the join is to a subclass
        # this is analogous to the
        # "_adjust_for_single_table_inheritance()" method in Query.

        if single_crit is not None:
            if secondaryjoin is not None:
                secondaryjoin = secondaryjoin & single_crit
            else:
                primaryjoin = primaryjoin & single_crit

        if aliased:
            if secondary is not None:
                secondary = secondary._anonymous_fromclause(flat=True)
                primary_aliasizer = ClauseAdapter(
                    secondary, exclude_fn=_ColInAnnotations("local")
                )
                secondary_aliasizer = ClauseAdapter(
                    dest_selectable, equivalents=self.child_equivalents
                ).chain(primary_aliasizer)
                if source_selectable is not None:
                    primary_aliasizer = ClauseAdapter(
                        secondary, exclude_fn=_ColInAnnotations("local")
                    ).chain(
                        ClauseAdapter(
                            source_selectable,
                            equivalents=self.parent_equivalents,
                        )
                    )

                secondaryjoin = secondary_aliasizer.traverse(secondaryjoin)
            else:
                primary_aliasizer = ClauseAdapter(
                    dest_selectable,
                    exclude_fn=_ColInAnnotations("local"),
                    equivalents=self.child_equivalents,
                )
                if source_selectable is not None:
                    primary_aliasizer.chain(
                        ClauseAdapter(
                            source_selectable,
                            exclude_fn=_ColInAnnotations("remote"),
                            equivalents=self.parent_equivalents,
                        )
                    )
                secondary_aliasizer = None

            primaryjoin = primary_aliasizer.traverse(primaryjoin)
            target_adapter = secondary_aliasizer or primary_aliasizer
            target_adapter.exclude_fn = None
        else:
            target_adapter = None
        return (
            primaryjoin,
            secondaryjoin,
            secondary,
            target_adapter,
            dest_selectable,
        )

    def create_lazy_clause(self, reverse_direction=False):
        binds = util.column_dict()
        equated_columns = util.column_dict()

        has_secondary = self.secondaryjoin is not None

        if has_secondary:
            lookup = collections.defaultdict(list)
            for l, r in self.local_remote_pairs:
                lookup[l].append((l, r))
                equated_columns[r] = l
        elif not reverse_direction:
            for l, r in self.local_remote_pairs:
                equated_columns[r] = l
        else:
            for l, r in self.local_remote_pairs:
                equated_columns[l] = r

        def col_to_bind(col):

            if (
                (not reverse_direction and "local" in col._annotations)
                or reverse_direction
                and (
                    (has_secondary and col in lookup)
                    or (not has_secondary and "remote" in col._annotations)
                )
            ):
                if col not in binds:
                    binds[col] = sql.bindparam(
                        None, None, type_=col.type, unique=True
                    )
                return binds[col]
            return None

        lazywhere = self.primaryjoin
        if self.secondaryjoin is None or not reverse_direction:
            lazywhere = visitors.replacement_traverse(
                lazywhere, {}, col_to_bind
            )

        if self.secondaryjoin is not None:
            secondaryjoin = self.secondaryjoin
            if reverse_direction:
                secondaryjoin = visitors.replacement_traverse(
                    secondaryjoin, {}, col_to_bind
                )
            lazywhere = sql.and_(lazywhere, secondaryjoin)

        bind_to_col = {binds[col].key: col for col in binds}

        return lazywhere, bind_to_col, equated_columns


class _ColInAnnotations(object):
    """Seralizable equivalent to:

        lambda c: "name" in c._annotations
    """

    def __init__(self, name):
        self.name = name

    def __call__(self, c):
        return self.name in c._annotations
