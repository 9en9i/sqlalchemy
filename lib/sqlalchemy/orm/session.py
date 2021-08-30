# orm/session.py
# Copyright (C) 2005-2021 the SQLAlchemy authors and contributors
# <see AUTHORS file>
#
# This module is part of SQLAlchemy and is released under
# the MIT License: https://www.opensource.org/licenses/mit-license.php
"""Provides the Session class and related utilities."""


import itertools
import sys
import weakref

from . import attributes
from . import context
from . import exc
from . import identity
from . import loading
from . import persistence
from . import query
from . import state as statelib
from .base import _class_to_mapper
from .base import _none_set
from .base import _state_mapper
from .base import instance_str
from .base import object_mapper
from .base import object_state
from .base import state_str
from .unitofwork import UOWTransaction
from .. import engine
from .. import exc as sa_exc
from .. import sql
from .. import util
from ..engine.util import TransactionalContext
from ..inspection import inspect
from ..sql import coercions
from ..sql import dml
from ..sql import roles
from ..sql import visitors
from ..sql.base import CompileState
from ..sql.selectable import LABEL_STYLE_TABLENAME_PLUS_COL

__all__ = [
    "Session",
    "SessionTransaction",
    "sessionmaker",
    "ORMExecuteState",
    "close_all_sessions",
    "make_transient",
    "make_transient_to_detached",
    "object_session",
]

_sessions = weakref.WeakValueDictionary()
"""Weak-referencing dictionary of :class:`.Session` objects.
"""

statelib._sessions = _sessions


def _state_session(state):
    """Given an :class:`.InstanceState`, return the :class:`.Session`
    associated, if any.
    """
    return state.session


class _SessionClassMethods(object):
    """Class-level methods for :class:`.Session`, :class:`.sessionmaker`."""

    @classmethod
    @util.deprecated(
        "1.3",
        "The :meth:`.Session.close_all` method is deprecated and will be "
        "removed in a future release.  Please refer to "
        ":func:`.session.close_all_sessions`.",
    )
    def close_all(cls):
        """Close *all* sessions in memory."""

        close_all_sessions()

    @classmethod
    @util.preload_module("sqlalchemy.orm.util")
    def identity_key(cls, *args, **kwargs):
        """Return an identity key.

        This is an alias of :func:`.util.identity_key`.

        """
        return util.preloaded.orm_util.identity_key(*args, **kwargs)

    @classmethod
    def object_session(cls, instance):
        """Return the :class:`.Session` to which an object belongs.

        This is an alias of :func:`.object_session`.

        """

        return object_session(instance)


ACTIVE = util.symbol("ACTIVE")
PREPARED = util.symbol("PREPARED")
COMMITTED = util.symbol("COMMITTED")
DEACTIVE = util.symbol("DEACTIVE")
CLOSED = util.symbol("CLOSED")


class ORMExecuteState(util.MemoizedSlots):
    """Represents a call to the :meth:`_orm.Session.execute` method, as passed
    to the :meth:`.SessionEvents.do_orm_execute` event hook.

    .. versionadded:: 1.4

    .. seealso::

        :ref:`session_execute_events` - top level documentation on how
        to use :meth:`_orm.SessionEvents.do_orm_execute`

    """

    __slots__ = (
        "session",
        "statement",
        "parameters",
        "execution_options",
        "local_execution_options",
        "bind_arguments",
        "_compile_state_cls",
        "_starting_event_idx",
        "_events_todo",
        "_update_execution_options",
    )

    def __init__(
        self,
        session,
        statement,
        parameters,
        execution_options,
        bind_arguments,
        compile_state_cls,
        events_todo,
    ):
        self.session = session
        self.statement = statement
        self.parameters = parameters
        self.local_execution_options = execution_options
        self.execution_options = statement._execution_options.union(
            execution_options
        )
        self.bind_arguments = bind_arguments
        self._compile_state_cls = compile_state_cls
        self._events_todo = list(events_todo)

    def _remaining_events(self):
        return self._events_todo[self._starting_event_idx + 1 :]

    def invoke_statement(
        self,
        statement=None,
        params=None,
        execution_options=None,
        bind_arguments=None,
    ):
        """Execute the statement represented by this
        :class:`.ORMExecuteState`, without re-invoking events that have
        already proceeded.

        This method essentially performs a re-entrant execution of the current
        statement for which the :meth:`.SessionEvents.do_orm_execute` event is
        being currently invoked.    The use case for this is for event handlers
        that want to override how the ultimate
        :class:`_engine.Result` object is returned, such as for schemes that
        retrieve results from an offline cache or which concatenate results
        from multiple executions.

        When the :class:`_engine.Result` object is returned by the actual
        handler function within :meth:`_orm.SessionEvents.do_orm_execute` and
        is propagated to the calling
        :meth:`_orm.Session.execute` method, the remainder of the
        :meth:`_orm.Session.execute` method is preempted and the
        :class:`_engine.Result` object is returned to the caller of
        :meth:`_orm.Session.execute` immediately.

        :param statement: optional statement to be invoked, in place of the
         statement currently represented by :attr:`.ORMExecuteState.statement`.

        :param params: optional dictionary of parameters which will be merged
         into the existing :attr:`.ORMExecuteState.parameters` of this
         :class:`.ORMExecuteState`.

        :param execution_options: optional dictionary of execution options
         will be merged into the existing
         :attr:`.ORMExecuteState.execution_options` of this
         :class:`.ORMExecuteState`.

        :param bind_arguments: optional dictionary of bind_arguments
         which will be merged amongst the current
         :attr:`.ORMExecuteState.bind_arguments`
         of this :class:`.ORMExecuteState`.

        :return: a :class:`_engine.Result` object with ORM-level results.

        .. seealso::

            :ref:`do_orm_execute_re_executing` - background and examples on the
            appropriate usage of :meth:`_orm.ORMExecuteState.invoke_statement`.


        """

        if statement is None:
            statement = self.statement

        _bind_arguments = dict(self.bind_arguments)
        if bind_arguments:
            _bind_arguments.update(bind_arguments)
        _bind_arguments["_sa_skip_events"] = True

        if params:
            _params = dict(self.parameters)
            _params.update(params)
        else:
            _params = self.parameters

        _execution_options = self.local_execution_options
        if execution_options:
            _execution_options = _execution_options.union(execution_options)

        return self.session.execute(
            statement,
            _params,
            _execution_options,
            _bind_arguments,
            _parent_execute_state=self,
        )

    @property
    def bind_mapper(self):
        """Return the :class:`_orm.Mapper` that is the primary "bind" mapper.

        For an :class:`_orm.ORMExecuteState` object invoking an ORM
        statement, that is, the :attr:`_orm.ORMExecuteState.is_orm_statement`
        attribute is ``True``, this attribute will return the
        :class:`_orm.Mapper` that is considered to be the "primary" mapper
        of the statement.   The term "bind mapper" refers to the fact that
        a :class:`_orm.Session` object may be "bound" to multiple
        :class:`_engine.Engine` objects keyed to mapped classes, and the
        "bind mapper" determines which of those :class:`_engine.Engine` objects
        would be selected.

        For a statement that is invoked against a single mapped class,
        :attr:`_orm.ORMExecuteState.bind_mapper` is intended to be a reliable
        way of getting this mapper.

        .. versionadded:: 1.4.0b2

        .. seealso::

            :attr:`_orm.ORMExecuteState.all_mappers`


        """
        return self.bind_arguments.get("mapper", None)

    @property
    def all_mappers(self):
        """Return a sequence of all :class:`_orm.Mapper` objects that are
        involved at the top level of this statement.

        By "top level" we mean those :class:`_orm.Mapper` objects that would
        be represented in the result set rows for a :func:`_sql.select`
        query, or for a :func:`_dml.update` or :func:`_dml.delete` query,
        the mapper that is the main subject of the UPDATE or DELETE.

        .. versionadded:: 1.4.0b2

        .. seealso::

            :attr:`_orm.ORMExecuteState.bind_mapper`



        """
        if not self.is_orm_statement:
            return []
        elif self.is_select:
            result = []
            seen = set()
            for d in self.statement.column_descriptions:
                ent = d["entity"]
                if ent:
                    insp = inspect(ent, raiseerr=False)
                    if insp and insp.mapper and insp.mapper not in seen:
                        seen.add(insp.mapper)
                        result.append(insp.mapper)
            return result
        elif self.is_update or self.is_delete:
            return [self.bind_mapper]
        else:
            return []

    @property
    def is_orm_statement(self):
        """return True if the operation is an ORM statement.

        This indicates that the select(), update(), or delete() being
        invoked contains ORM entities as subjects.   For a statement
        that does not have ORM entities and instead refers only to
        :class:`.Table` metadata, it is invoked as a Core SQL statement
        and no ORM-level automation takes place.

        """
        return self._compile_state_cls is not None

    @property
    def is_select(self):
        """return True if this is a SELECT operation."""
        return self.statement.is_select

    @property
    def is_insert(self):
        """return True if this is an INSERT operation."""
        return self.statement.is_dml and self.statement.is_insert

    @property
    def is_update(self):
        """return True if this is an UPDATE operation."""
        return self.statement.is_dml and self.statement.is_update

    @property
    def is_delete(self):
        """return True if this is a DELETE operation."""
        return self.statement.is_dml and self.statement.is_delete

    @property
    def _is_crud(self):
        return isinstance(self.statement, (dml.Update, dml.Delete))

    def update_execution_options(self, **opts):
        # TODO: no coverage
        self.local_execution_options = self.local_execution_options.union(opts)

    def _orm_compile_options(self):
        if not self.is_select:
            return None
        opts = self.statement._compile_options
        if opts.isinstance(context.ORMCompileState.default_compile_options):
            return opts
        else:
            return None

    @property
    def lazy_loaded_from(self):
        """An :class:`.InstanceState` that is using this statement execution
        for a lazy load operation.

        The primary rationale for this attribute is to support the horizontal
        sharding extension, where it is available within specific query
        execution time hooks created by this extension.   To that end, the
        attribute is only intended to be meaningful at **query execution
        time**, and importantly not any time prior to that, including query
        compilation time.

        """
        return self.load_options._lazy_loaded_from

    @property
    def loader_strategy_path(self):
        """Return the :class:`.PathRegistry` for the current load path.

        This object represents the "path" in a query along relationships
        when a particular object or collection is being loaded.

        """
        opts = self._orm_compile_options()
        if opts is not None:
            return opts._current_path
        else:
            return None

    @property
    def is_column_load(self):
        """Return True if the operation is refreshing column-oriented
        attributes on an existing ORM object.

        This occurs during operations such as :meth:`_orm.Session.refresh`,
        as well as when an attribute deferred by :func:`_orm.defer` is
        being loaded, or an attribute that was expired either directly
        by :meth:`_orm.Session.expire` or via a commit operation is being
        loaded.

        Handlers will very likely not want to add any options to queries
        when such an operation is occurring as the query should be a straight
        primary key fetch which should not have any additional WHERE criteria,
        and loader options travelling with the instance
        will have already been added to the query.

        .. versionadded:: 1.4.0b2

        .. seealso::

            :attr:`_orm.ORMExecuteState.is_relationship_load`

        """
        opts = self._orm_compile_options()
        return opts is not None and opts._for_refresh_state

    @property
    def is_relationship_load(self):
        """Return True if this load is loading objects on behalf of a
        relationship.

        This means, the loader in effect is either a LazyLoader,
        SelectInLoader, SubqueryLoader, or similar, and the entire
        SELECT statement being emitted is on behalf of a relationship
        load.

        Handlers will very likely not want to add any options to queries
        when such an operation is occurring, as loader options are already
        capable of being propagated to relationship loaders and should
        be already present.

        .. seealso::

            :attr:`_orm.ORMExecuteState.is_column_load`

        """
        opts = self._orm_compile_options()
        if opts is None:
            return False
        path = self.loader_strategy_path
        return path is not None and not path.is_root

    @property
    def load_options(self):
        """Return the load_options that will be used for this execution."""

        if not self.is_select:
            raise sa_exc.InvalidRequestError(
                "This ORM execution is not against a SELECT statement "
                "so there are no load options."
            )
        return self.execution_options.get(
            "_sa_orm_load_options", context.QueryContext.default_load_options
        )

    @property
    def update_delete_options(self):
        """Return the update_delete_options that will be used for this
        execution."""

        if not self._is_crud:
            raise sa_exc.InvalidRequestError(
                "This ORM execution is not against an UPDATE or DELETE "
                "statement so there are no update options."
            )
        return self.execution_options.get(
            "_sa_orm_update_options",
            persistence.BulkUDCompileState.default_update_options,
        )

    @property
    def user_defined_options(self):
        """The sequence of :class:`.UserDefinedOptions` that have been
        associated with the statement being invoked.

        """
        return [
            opt
            for opt in self.statement._with_options
            if not opt._is_compile_state and not opt._is_legacy_option
        ]


class SessionTransaction(TransactionalContext):
    """A :class:`.Session`-level transaction.

    :class:`.SessionTransaction` is produced from the
    :meth:`_orm.Session.begin`
    and :meth:`_orm.Session.begin_nested` methods.   It's largely an internal
    object that in modern use provides a context manager for session
    transactions.

    Documentation on interacting with :class:`_orm.SessionTransaction` is
    at: :ref:`unitofwork_transaction`.


    .. versionchanged:: 1.4  The scoping and API methods to work with the
       :class:`_orm.SessionTransaction` object directly have been simplified.

    .. seealso::

        :ref:`unitofwork_transaction`

        :meth:`.Session.begin`

        :meth:`.Session.begin_nested`

        :meth:`.Session.rollback`

        :meth:`.Session.commit`

        :meth:`.Session.in_transaction`

        :meth:`.Session.in_nested_transaction`

        :meth:`.Session.get_transaction`

        :meth:`.Session.get_nested_transaction`


    """

    _rollback_exception = None

    def __init__(
        self,
        session,
        parent=None,
        nested=False,
        autobegin=False,
    ):
        TransactionalContext._trans_ctx_check(session)

        self.session = session
        self._connections = {}
        self._parent = parent
        self.nested = nested
        if nested:
            self._previous_nested_transaction = session._nested_transaction
        self._state = ACTIVE
        if not parent and nested:
            raise sa_exc.InvalidRequestError(
                "Can't start a SAVEPOINT transaction when no existing "
                "transaction is in progress"
            )

        self._take_snapshot(autobegin=autobegin)

        # make sure transaction is assigned before we call the
        # dispatch
        self.session._transaction = self

        self.session.dispatch.after_transaction_create(self.session, self)

    @property
    def parent(self):
        """The parent :class:`.SessionTransaction` of this
        :class:`.SessionTransaction`.

        If this attribute is ``None``, indicates this
        :class:`.SessionTransaction` is at the top of the stack, and
        corresponds to a real "COMMIT"/"ROLLBACK"
        block.  If non-``None``, then this is either a "subtransaction"
        or a "nested" / SAVEPOINT transaction.  If the
        :attr:`.SessionTransaction.nested` attribute is ``True``, then
        this is a SAVEPOINT, and if ``False``, indicates this a subtransaction.

        .. versionadded:: 1.0.16 - use ._parent for previous versions

        """
        return self._parent

    nested = False
    """Indicates if this is a nested, or SAVEPOINT, transaction.

    When :attr:`.SessionTransaction.nested` is True, it is expected
    that :attr:`.SessionTransaction.parent` will be True as well.

    """

    @property
    def is_active(self):
        return self.session is not None and self._state is ACTIVE

    def _assert_active(
        self,
        prepared_ok=False,
        rollback_ok=False,
        deactive_ok=False,
        closed_msg="This transaction is closed",
    ):
        if self._state is COMMITTED:
            raise sa_exc.InvalidRequestError(
                "This session is in 'committed' state; no further "
                "SQL can be emitted within this transaction."
            )
        elif self._state is PREPARED:
            if not prepared_ok:
                raise sa_exc.InvalidRequestError(
                    "This session is in 'prepared' state; no further "
                    "SQL can be emitted within this transaction."
                )
        elif self._state is DEACTIVE:
            if not deactive_ok and not rollback_ok:
                if self._rollback_exception:
                    raise sa_exc.PendingRollbackError(
                        "This Session's transaction has been rolled back "
                        "due to a previous exception during flush."
                        " To begin a new transaction with this Session, "
                        "first issue Session.rollback()."
                        " Original exception was: %s"
                        % self._rollback_exception,
                        code="7s2a",
                    )
                elif not deactive_ok:
                    raise sa_exc.InvalidRequestError(
                        "This session is in 'inactive' state, due to the "
                        "SQL transaction being rolled back; no further "
                        "SQL can be emitted within this transaction."
                    )
        elif self._state is CLOSED:
            raise sa_exc.ResourceClosedError(closed_msg)

    @property
    def _is_transaction_boundary(self):
        return self.nested or not self._parent

    def connection(self, bindkey, execution_options=None, **kwargs):
        self._assert_active()
        bind = self.session.get_bind(bindkey, **kwargs)
        return self._connection_for_bind(bind, execution_options)

    def _begin(self, nested=False):
        self._assert_active()
        return SessionTransaction(self.session, self, nested=nested)

    def _iterate_self_and_parents(self, upto=None):

        current = self
        result = ()
        while current:
            result += (current,)
            if current._parent is upto:
                break
            elif current._parent is None:
                raise sa_exc.InvalidRequestError(
                    "Transaction %s is not on the active transaction list"
                    % (upto)
                )
            else:
                current = current._parent

        return result

    def _take_snapshot(self, autobegin=False):
        if not self._is_transaction_boundary:
            self._new = self._parent._new
            self._deleted = self._parent._deleted
            self._dirty = self._parent._dirty
            self._key_switches = self._parent._key_switches
            return

        if not autobegin and not self.session._flushing:
            self.session.flush()

        self._new = weakref.WeakKeyDictionary()
        self._deleted = weakref.WeakKeyDictionary()
        self._dirty = weakref.WeakKeyDictionary()
        self._key_switches = weakref.WeakKeyDictionary()

    def _restore_snapshot(self, dirty_only=False):
        """Restore the restoration state taken before a transaction began.

        Corresponds to a rollback.

        """
        assert self._is_transaction_boundary

        to_expunge = set(self._new).union(self.session._new)
        self.session._expunge_states(to_expunge, to_transient=True)

        for s, (oldkey, newkey) in self._key_switches.items():
            # we probably can do this conditionally based on
            # if we expunged or not, but safe_discard does that anyway
            self.session.identity_map.safe_discard(s)

            # restore the old key
            s.key = oldkey

            # now restore the object, but only if we didn't expunge
            if s not in to_expunge:
                self.session.identity_map.replace(s)

        for s in set(self._deleted).union(self.session._deleted):
            self.session._update_impl(s, revert_deletion=True)

        assert not self.session._deleted

        for s in self.session.identity_map.all_states():
            if not dirty_only or s.modified or s in self._dirty:
                s._expire(s.dict, self.session.identity_map._modified)

    def _remove_snapshot(self):
        """Remove the restoration state taken before a transaction began.

        Corresponds to a commit.

        """
        assert self._is_transaction_boundary

        if not self.nested and self.session.expire_on_commit:
            for s in self.session.identity_map.all_states():
                s._expire(s.dict, self.session.identity_map._modified)

            statelib.InstanceState._detach_states(
                list(self._deleted), self.session
            )
            self._deleted.clear()
        elif self.nested:
            self._parent._new.update(self._new)
            self._parent._dirty.update(self._dirty)
            self._parent._deleted.update(self._deleted)
            self._parent._key_switches.update(self._key_switches)

    def _connection_for_bind(self, bind, execution_options):
        self._assert_active()

        if bind in self._connections:
            if execution_options:
                util.warn(
                    "Connection is already established for the "
                    "given bind; execution_options ignored"
                )
            return self._connections[bind][0]

        local_connect = False
        should_commit = True

        if self._parent:
            conn = self._parent._connection_for_bind(bind, execution_options)
            if not self.nested:
                return conn
        else:
            if isinstance(bind, engine.Connection):
                conn = bind
                if conn.engine in self._connections:
                    raise sa_exc.InvalidRequestError(
                        "Session already has a Connection associated for the "
                        "given Connection's Engine"
                    )
            else:
                conn = bind.connect()
                local_connect = True

        try:
            if execution_options:
                conn = conn.execution_options(**execution_options)

            if self.session.twophase and self._parent is None:
                transaction = conn.begin_twophase()
            elif self.nested:
                transaction = conn.begin_nested()
            elif conn.in_transaction():
                # if given a future connection already in a transaction, don't
                # commit that transaction unless it is a savepoint
                if conn.in_nested_transaction():
                    transaction = conn.get_nested_transaction()
                else:
                    transaction = conn.get_transaction()
                    should_commit = False
            else:
                transaction = conn.begin()
        except:
            # connection will not not be associated with this Session;
            # close it immediately so that it isn't closed under GC
            if local_connect:
                conn.close()
            raise
        else:
            bind_is_connection = isinstance(bind, engine.Connection)

            self._connections[conn] = self._connections[conn.engine] = (
                conn,
                transaction,
                should_commit,
                not bind_is_connection,
            )
            self.session.dispatch.after_begin(self.session, self, conn)
            return conn

    def prepare(self):
        if self._parent is not None or not self.session.twophase:
            raise sa_exc.InvalidRequestError(
                "'twophase' mode not enabled, or not root transaction; "
                "can't prepare."
            )
        self._prepare_impl()

    def _prepare_impl(self):
        self._assert_active()
        if self._parent is None or self.nested:
            self.session.dispatch.before_commit(self.session)

        stx = self.session._transaction
        if stx is not self:
            for subtransaction in stx._iterate_self_and_parents(upto=self):
                subtransaction.commit()

        if not self.session._flushing:
            for _flush_guard in range(100):
                if self.session._is_clean():
                    break
                self.session.flush()
            else:
                raise exc.FlushError(
                    "Over 100 subsequent flushes have occurred within "
                    "session.commit() - is an after_flush() hook "
                    "creating new objects?"
                )

        if self._parent is None and self.session.twophase:
            try:
                for t in set(self._connections.values()):
                    t[1].prepare()
            except:
                with util.safe_reraise():
                    self.rollback()

        self._state = PREPARED

    def commit(self, _to_root=False):
        self._assert_active(prepared_ok=True)
        if self._state is not PREPARED:
            self._prepare_impl()

        if self._parent is None or self.nested:
            for conn, trans, should_commit, autoclose in set(
                self._connections.values()
            ):
                if should_commit:
                    trans.commit()

            self._state = COMMITTED
            self.session.dispatch.after_commit(self.session)

            self._remove_snapshot()

        self.close()

        if _to_root and self._parent:
            return self._parent.commit(_to_root=True)

        return self._parent

    def rollback(self, _capture_exception=False, _to_root=False):
        self._assert_active(prepared_ok=True, rollback_ok=True)

        stx = self.session._transaction
        if stx is not self:
            for subtransaction in stx._iterate_self_and_parents(upto=self):
                subtransaction.close()

        boundary = self
        rollback_err = None
        if self._state in (ACTIVE, PREPARED):
            for transaction in self._iterate_self_and_parents():
                if transaction._parent is None or transaction.nested:
                    try:
                        for t in set(transaction._connections.values()):
                            t[1].rollback()

                        transaction._state = DEACTIVE
                        self.session.dispatch.after_rollback(self.session)
                    except:
                        rollback_err = sys.exc_info()
                    finally:
                        transaction._state = DEACTIVE
                        transaction._restore_snapshot(
                            dirty_only=transaction.nested
                        )
                    boundary = transaction
                    break
                else:
                    transaction._state = DEACTIVE

        sess = self.session

        if not rollback_err and not sess._is_clean():

            # if items were added, deleted, or mutated
            # here, we need to re-restore the snapshot
            util.warn(
                "Session's state has been changed on "
                "a non-active transaction - this state "
                "will be discarded."
            )
            boundary._restore_snapshot(dirty_only=boundary.nested)

        self.close()

        if self._parent and _capture_exception:
            self._parent._rollback_exception = sys.exc_info()[1]

        if rollback_err:
            util.raise_(rollback_err[1], with_traceback=rollback_err[2])

        sess.dispatch.after_soft_rollback(sess, self)

        if _to_root and self._parent:
            return self._parent.rollback(_to_root=True)
        return self._parent

    def close(self, invalidate=False):
        if self.nested:
            self.session._nested_transaction = (
                self._previous_nested_transaction
            )

        self.session._transaction = self._parent

        if self._parent is None:
            for connection, transaction, should_commit, autoclose in set(
                self._connections.values()
            ):
                if invalidate:
                    connection.invalidate()
                if should_commit and transaction.is_active:
                    transaction.close()
                if autoclose:
                    connection.close()

        self._state = CLOSED
        self.session.dispatch.after_transaction_end(self.session, self)

        self.session = None
        self._connections = None

    def _get_subject(self):
        return self.session

    def _transaction_is_active(self):
        return self._state is ACTIVE

    def _transaction_is_closed(self):
        return self._state is CLOSED


class Session(_SessionClassMethods):
    """Manages persistence operations for ORM-mapped objects.

    The Session's usage paradigm is described at :doc:`/orm/session`.


    """

    _is_asyncio = False

    @util.deprecated_params(
        autocommit=(
            "2.0",
            "The :paramref:`.Session.autocommit` parameter is deprecated "
            "and will be removed in SQLAlchemy version 2.0.  The "
            ':class:`_orm.Session` now features "autobegin" behavior '
            "such that the :meth:`.Session.begin` method may be called "
            "if a transaction has not yet been started yet.  See the section "
            ":ref:`session_explicit_begin` for background.",
        ),
    )
    def __init__(
        self,
        bind=None,
        autoflush=True,
        future=False,
        expire_on_commit=True,
        autocommit=False,
        twophase=False,
        binds=None,
        enable_baked_queries=True,
        info=None,
        query_cls=None,
    ):
        r"""Construct a new Session.

        See also the :class:`.sessionmaker` function which is used to
        generate a :class:`.Session`-producing callable with a given
        set of arguments.

        :param autocommit:
          Defaults to ``False``. When ``True``, the
          :class:`.Session` does not automatically begin transactions for
          individual statement executions, will acquire connections from the
          engine on an as-needed basis, releasing to the connection pool
          after each statement. Flushes will begin and commit (or possibly
          rollback) their own transaction if no transaction is present.
          When using this mode, the
          :meth:`.Session.begin` method may be used to explicitly start
          transactions, but the usual "autobegin" behavior is not present.

        :param autoflush: When ``True``, all query operations will issue a
           :meth:`~.Session.flush` call to this ``Session`` before proceeding.
           This is a convenience feature so that :meth:`~.Session.flush` need
           not be called repeatedly in order for database queries to retrieve
           results. It's typical that ``autoflush`` is used in conjunction
           with ``autocommit=False``. In this scenario, explicit calls to
           :meth:`~.Session.flush` are rarely needed; you usually only need to
           call :meth:`~.Session.commit` (which flushes) to finalize changes.

        :param bind: An optional :class:`_engine.Engine` or
           :class:`_engine.Connection` to
           which this ``Session`` should be bound. When specified, all SQL
           operations performed by this session will execute via this
           connectable.

        :param binds: A dictionary which may specify any number of
           :class:`_engine.Engine` or :class:`_engine.Connection`
           objects as the source of
           connectivity for SQL operations on a per-entity basis.   The keys
           of the dictionary consist of any series of mapped classes,
           arbitrary Python classes that are bases for mapped classes,
           :class:`_schema.Table` objects and :class:`_orm.Mapper` objects.
           The
           values of the dictionary are then instances of
           :class:`_engine.Engine`
           or less commonly :class:`_engine.Connection` objects.
           Operations which
           proceed relative to a particular mapped class will consult this
           dictionary for the closest matching entity in order to determine
           which :class:`_engine.Engine` should be used for a particular SQL
           operation.    The complete heuristics for resolution are
           described at :meth:`.Session.get_bind`.  Usage looks like::

            Session = sessionmaker(binds={
                SomeMappedClass: create_engine('postgresql://engine1'),
                SomeDeclarativeBase: create_engine('postgresql://engine2'),
                some_mapper: create_engine('postgresql://engine3'),
                some_table: create_engine('postgresql://engine4'),
                })

           .. seealso::

                :ref:`session_partitioning`

                :meth:`.Session.bind_mapper`

                :meth:`.Session.bind_table`

                :meth:`.Session.get_bind`


        :param \class_: Specify an alternate class other than
           ``sqlalchemy.orm.session.Session`` which should be used by the
           returned class. This is the only argument that is local to the
           :class:`.sessionmaker` function, and is not sent directly to the
           constructor for ``Session``.

        :param enable_baked_queries: defaults to ``True``.  A flag consumed
           by the :mod:`sqlalchemy.ext.baked` extension to determine if
           "baked queries" should be cached, as is the normal operation
           of this extension.  When set to ``False``, caching as used by
           this particular extension is disabled.

           .. versionchanged:: 1.4 The ``sqlalchemy.ext.baked`` extension is
              legacy and is not used by any of SQLAlchemy's internals. This
              flag therefore only affects applications that are making explicit
              use of this extension within their own code.

        :param expire_on_commit:  Defaults to ``True``. When ``True``, all
           instances will be fully expired after each :meth:`~.commit`,
           so that all attribute/object access subsequent to a completed
           transaction will load from the most recent database state.

            .. seealso::

                :ref:`session_committing`

        :param future: if True, use 2.0 style transactional and engine
          behavior.  Future mode includes the following behaviors:

          * The :class:`_orm.Session` will not use "bound" metadata in order
            to locate an :class:`_engine.Engine`; the engine or engines in use
            must be specified to the constructor of :class:`_orm.Session` or
            otherwise be configured against the :class:`_orm.sessionmaker`
            in use

          * The "subtransactions" feature of :meth:`_orm.Session.begin` is
            removed in version 2.0 and is disabled when the future flag is
            set.

          * The behavior of the :paramref:`_orm.relationship.cascade_backrefs`
            flag on a :func:`_orm.relationship` will always assume
            "False" behavior.

          .. versionadded:: 1.4

          .. seealso::

            :ref:`migration_20_toplevel`

        :param info: optional dictionary of arbitrary data to be associated
           with this :class:`.Session`.  Is available via the
           :attr:`.Session.info` attribute.  Note the dictionary is copied at
           construction time so that modifications to the per-
           :class:`.Session` dictionary will be local to that
           :class:`.Session`.

        :param query_cls:  Class which should be used to create new Query
          objects, as returned by the :meth:`~.Session.query` method.
          Defaults to :class:`_query.Query`.

        :param twophase:  When ``True``, all transactions will be started as
            a "two phase" transaction, i.e. using the "two phase" semantics
            of the database in use along with an XID.  During a
            :meth:`~.commit`, after :meth:`~.flush` has been issued for all
            attached databases, the :meth:`~.TwoPhaseTransaction.prepare`
            method on each database's :class:`.TwoPhaseTransaction` will be
            called. This allows each database to roll back the entire
            transaction, before each transaction is committed.

        """
        self.identity_map = identity.WeakInstanceDict()

        self._new = {}  # InstanceState->object, strong refs object
        self._deleted = {}  # same
        self.bind = bind
        self.__binds = {}
        self._flushing = False
        self._warn_on_events = False
        self._transaction = None
        self._nested_transaction = None
        self.future = future
        self.hash_key = _new_sessionid()
        self.autoflush = autoflush
        self.expire_on_commit = expire_on_commit
        self.enable_baked_queries = enable_baked_queries

        if autocommit:
            if future:
                raise sa_exc.ArgumentError(
                    "Cannot use autocommit mode with future=True."
                )
            self.autocommit = True
        else:
            self.autocommit = False

        self.twophase = twophase
        self._query_cls = query_cls if query_cls else query.Query
        if info:
            self.info.update(info)

        if binds is not None:
            for key, bind in binds.items():
                self._add_bind(key, bind)

        _sessions[self.hash_key] = self

    # used by sqlalchemy.engine.util.TransactionalContext
    _trans_context_manager = None

    connection_callable = None

    def __enter__(self):
        return self

    def __exit__(self, type_, value, traceback):
        self.close()

    @util.contextmanager
    def _maker_context_manager(self):
        with self:
            with self.begin():
                yield self

    @property
    @util.deprecated_20(
        ":attr:`_orm.Session.transaction`",
        alternative="For context manager use, use "
        ":meth:`_orm.Session.begin`.  To access "
        "the current root transaction, use "
        ":meth:`_orm.Session.get_transaction`.",
        warn_on_attribute_access=True,
    )
    def transaction(self):
        """The current active or inactive :class:`.SessionTransaction`.

        May be None if no transaction has begun yet.

        .. versionchanged:: 1.4  the :attr:`.Session.transaction` attribute
           is now a read-only descriptor that also may return None if no
           transaction has begun yet.


        """
        return self._legacy_transaction()

    def _legacy_transaction(self):
        if not self.future:
            self._autobegin()
        return self._transaction

    def in_transaction(self):
        """Return True if this :class:`_orm.Session` has begun a transaction.

        .. versionadded:: 1.4

        .. seealso::

            :attr:`_orm.Session.is_active`


        """
        return self._transaction is not None

    def in_nested_transaction(self):
        """Return True if this :class:`_orm.Session` has begun a nested
        transaction, e.g. SAVEPOINT.

        .. versionadded:: 1.4

        """
        return self._nested_transaction is not None

    def get_transaction(self):
        """Return the current root transaction in progress, if any.

        .. versionadded:: 1.4

        """
        trans = self._transaction
        while trans is not None and trans._parent is not None:
            trans = trans._parent
        return trans

    def get_nested_transaction(self):
        """Return the current nested transaction in progress, if any.

        .. versionadded:: 1.4

        """

        return self._nested_transaction

    @util.memoized_property
    def info(self):
        """A user-modifiable dictionary.

        The initial value of this dictionary can be populated using the
        ``info`` argument to the :class:`.Session` constructor or
        :class:`.sessionmaker` constructor or factory methods.  The dictionary
        here is always local to this :class:`.Session` and can be modified
        independently of all other :class:`.Session` objects.

        """
        return {}

    def _autobegin(self):
        if not self.autocommit and self._transaction is None:

            trans = SessionTransaction(self, autobegin=True)
            assert self._transaction is trans
            return True

        return False

    @util.deprecated_params(
        subtransactions=(
            "2.0",
            "The :paramref:`_orm.Session.begin.subtransactions` flag is "
            "deprecated and "
            "will be removed in SQLAlchemy version 2.0.  See "
            "the documentation at :ref:`session_subtransactions` for "
            "background on a compatible alternative pattern.",
        )
    )
    def begin(self, subtransactions=False, nested=False, _subtrans=False):
        """Begin a transaction, or nested transaction,
        on this :class:`.Session`, if one is not already begun.

        The :class:`_orm.Session` object features **autobegin** behavior,
        so that normally it is not necessary to call the
        :meth:`_orm.Session.begin`
        method explicitly. However, it may be used in order to control
        the scope of when the transactional state is begun.

        When used to begin the outermost transaction, an error is raised
        if this :class:`.Session` is already inside of a transaction.

        :param nested: if True, begins a SAVEPOINT transaction and is
         equivalent to calling :meth:`~.Session.begin_nested`. For
         documentation on SAVEPOINT transactions, please see
         :ref:`session_begin_nested`.

        :param subtransactions: if True, indicates that this
         :meth:`~.Session.begin` can create a "subtransaction".

        :return: the :class:`.SessionTransaction` object.  Note that
         :class:`.SessionTransaction`
         acts as a Python context manager, allowing :meth:`.Session.begin`
         to be used in a "with" block.  See :ref:`session_autocommit` for
         an example.

        .. seealso::

            :ref:`session_autobegin`

            :ref:`unitofwork_transaction`

            :meth:`.Session.begin_nested`


        """

        if subtransactions and self.future:
            raise NotImplementedError(
                "subtransactions are not implemented in future "
                "Session objects."
            )

        if self._autobegin():
            if not subtransactions and not nested and not _subtrans:
                return self._transaction

        if self._transaction is not None:
            if subtransactions or _subtrans or nested:
                trans = self._transaction._begin(nested=nested)
                assert self._transaction is trans
                if nested:
                    self._nested_transaction = trans
            else:
                raise sa_exc.InvalidRequestError(
                    "A transaction is already begun on this Session."
                )
        elif not self.autocommit:
            # outermost transaction.  must be a not nested and not
            # a subtransaction

            assert not nested and not _subtrans and not subtransactions
            trans = SessionTransaction(self)
            assert self._transaction is trans
        else:
            # legacy autocommit mode
            assert not self.future
            trans = SessionTransaction(self, nested=nested)
            assert self._transaction is trans

        return self._transaction  # needed for __enter__/__exit__ hook

    def begin_nested(self):
        """Begin a "nested" transaction on this Session, e.g. SAVEPOINT.

        The target database(s) and associated drivers must support SQL
        SAVEPOINT for this method to function correctly.

        For documentation on SAVEPOINT
        transactions, please see :ref:`session_begin_nested`.

        :return: the :class:`.SessionTransaction` object.  Note that
         :class:`.SessionTransaction` acts as a context manager, allowing
         :meth:`.Session.begin_nested` to be used in a "with" block.
         See :ref:`session_begin_nested` for a usage example.

        .. seealso::

            :ref:`session_begin_nested`

            :ref:`pysqlite_serializable` - special workarounds required
            with the SQLite driver in order for SAVEPOINT to work
            correctly.

        """
        return self.begin(nested=True)

    def rollback(self):
        """Rollback the current transaction in progress.

        If no transaction is in progress, this method is a pass-through.

        In :term:`1.x-style` use, this method rolls back the topmost
        database transaction if no nested transactions are in effect, or
        to the current nested transaction if one is in effect.

        When
        :term:`2.0-style` use is in effect via the
        :paramref:`_orm.Session.future` flag, the method always rolls back
        the topmost database transaction, discarding any nested
        transactions that may be in progress.

        .. seealso::

            :ref:`session_rollback`

            :ref:`unitofwork_transaction`

        """
        if self._transaction is None:
            pass
        else:
            self._transaction.rollback(_to_root=self.future)

    def commit(self):
        """Flush pending changes and commit the current transaction.

        If no transaction is in progress, the method will first
        "autobegin" a new transaction and commit.

        If :term:`1.x-style` use is in effect and there are currently
        SAVEPOINTs in progress via :meth:`_orm.Session.begin_nested`,
        the operation will release the current SAVEPOINT but not commit
        the outermost database transaction.

        If :term:`2.0-style` use is in effect via the
        :paramref:`_orm.Session.future` flag, the outermost database
        transaction is committed unconditionally, automatically releasing any
        SAVEPOINTs in effect.

        When using legacy "autocommit" mode, this method is only
        valid to call if a transaction is actually in progress, else
        an error is raised.   Similarly, when using legacy "subtransactions",
        the method will instead close out the current "subtransaction",
        rather than the actual database transaction, if a transaction
        is in progress.

        .. seealso::

            :ref:`session_committing`

            :ref:`unitofwork_transaction`

        """
        if self._transaction is None:
            if not self._autobegin():
                raise sa_exc.InvalidRequestError("No transaction is begun.")

        self._transaction.commit(_to_root=self.future)

    def prepare(self):
        """Prepare the current transaction in progress for two phase commit.

        If no transaction is in progress, this method raises an
        :exc:`~sqlalchemy.exc.InvalidRequestError`.

        Only root transactions of two phase sessions can be prepared. If the
        current transaction is not such, an
        :exc:`~sqlalchemy.exc.InvalidRequestError` is raised.

        """
        if self._transaction is None:
            if not self._autobegin():
                raise sa_exc.InvalidRequestError("No transaction is begun.")

        self._transaction.prepare()

    def connection(
        self,
        bind_arguments=None,
        close_with_result=False,
        execution_options=None,
        **kw
    ):
        r"""Return a :class:`_engine.Connection` object corresponding to this
        :class:`.Session` object's transactional state.

        If this :class:`.Session` is configured with ``autocommit=False``,
        either the :class:`_engine.Connection` corresponding to the current
        transaction is returned, or if no transaction is in progress, a new
        one is begun and the :class:`_engine.Connection`
        returned (note that no
        transactional state is established with the DBAPI until the first
        SQL statement is emitted).

        Alternatively, if this :class:`.Session` is configured with
        ``autocommit=True``, an ad-hoc :class:`_engine.Connection` is returned
        using :meth:`_engine.Engine.connect` on the underlying
        :class:`_engine.Engine`.

        Ambiguity in multi-bind or unbound :class:`.Session` objects can be
        resolved through any of the optional keyword arguments.   This
        ultimately makes usage of the :meth:`.get_bind` method for resolution.

        :param bind_arguments: dictionary of bind arguments.  May include
         "mapper", "bind", "clause", other custom arguments that are passed
         to :meth:`.Session.get_bind`.

        :param bind:
          deprecated; use bind_arguments

        :param mapper:
          deprecated; use bind_arguments

        :param clause:
          deprecated; use bind_arguments

        :param close_with_result: Passed to :meth:`_engine.Engine.connect`,
          indicating the :class:`_engine.Connection` should be considered
          "single use", automatically closing when the first result set is
          closed.  This flag only has an effect if this :class:`.Session` is
          configured with ``autocommit=True`` and does not already have a
          transaction in progress.

          .. deprecated:: 1.4  this parameter is deprecated and will be removed
             in SQLAlchemy 2.0

        :param execution_options: a dictionary of execution options that will
         be passed to :meth:`_engine.Connection.execution_options`, **when the
         connection is first procured only**.   If the connection is already
         present within the :class:`.Session`, a warning is emitted and
         the arguments are ignored.

         .. seealso::

            :ref:`session_transaction_isolation`

        :param \**kw:
          deprecated; use bind_arguments

        """

        if not bind_arguments:
            bind_arguments = kw

        bind = bind_arguments.pop("bind", None)
        if bind is None:
            bind = self.get_bind(**bind_arguments)

        return self._connection_for_bind(
            bind,
            close_with_result=close_with_result,
            execution_options=execution_options,
        )

    def _connection_for_bind(self, engine, execution_options=None, **kw):
        TransactionalContext._trans_ctx_check(self)

        if self._transaction is not None or self._autobegin():
            return self._transaction._connection_for_bind(
                engine, execution_options
            )

        assert self._transaction is None
        assert self.autocommit
        conn = engine.connect(**kw)
        if execution_options:
            conn = conn.execution_options(**execution_options)
        return conn

    def execute(
        self,
        statement,
        params=None,
        execution_options=util.EMPTY_DICT,
        bind_arguments=None,
        _parent_execute_state=None,
        _add_event=None,
        **kw
    ):
        r"""Execute a SQL expression construct.

        Returns a :class:`_engine.Result` object representing
        results of the statement execution.

        E.g.::

            from sqlalchemy import select
            result = session.execute(
                select(User).where(User.id == 5)
            )

        The API contract of :meth:`_orm.Session.execute` is similar to that
        of :meth:`_future.Connection.execute`, the :term:`2.0 style` version
        of :class:`_future.Connection`.

        .. versionchanged:: 1.4 the :meth:`_orm.Session.execute` method is
           now the primary point of ORM statement execution when using
           :term:`2.0 style` ORM usage.

        :param statement:
            An executable statement (i.e. an :class:`.Executable` expression
            such as :func:`_expression.select`).

        :param params:
            Optional dictionary, or list of dictionaries, containing
            bound parameter values.   If a single dictionary, single-row
            execution occurs; if a list of dictionaries, an
            "executemany" will be invoked.  The keys in each dictionary
            must correspond to parameter names present in the statement.

        :param execution_options: optional dictionary of execution options,
         which will be associated with the statement execution.  This
         dictionary can provide a subset of the options that are accepted
         by :meth:`_engine.Connection.execution_options`, and may also
         provide additional options understood only in an ORM context.

        :param bind_arguments: dictionary of additional arguments to determine
         the bind.  May include "mapper", "bind", or other custom arguments.
         Contents of this dictionary are passed to the
         :meth:`.Session.get_bind` method.

        :param mapper:
          deprecated; use the bind_arguments dictionary

        :param bind:
          deprecated; use the bind_arguments dictionary

        :param \**kw:
          deprecated; use the bind_arguments dictionary

        :return: a :class:`_engine.Result` object.


        """
        statement = coercions.expect(roles.StatementRole, statement)

        if kw:
            util.warn_deprecated_20(
                "Passing bind arguments to Session.execute() as keyword "
                "arguments is deprecated and will be removed SQLAlchemy 2.0. "
                "Please use the bind_arguments parameter."
            )
            if not bind_arguments:
                bind_arguments = kw
            else:
                bind_arguments.update(kw)
        elif not bind_arguments:
            bind_arguments = {}

        if (
            statement._propagate_attrs.get("compile_state_plugin", None)
            == "orm"
        ):
            # note that even without "future" mode, we need
            compile_state_cls = CompileState._get_plugin_class_for_plugin(
                statement, "orm"
            )
        else:
            compile_state_cls = None

        execution_options = util.coerce_to_immutabledict(execution_options)

        if compile_state_cls is not None:
            (
                statement,
                execution_options,
            ) = compile_state_cls.orm_pre_session_exec(
                self,
                statement,
                params,
                execution_options,
                bind_arguments,
                _parent_execute_state is not None,
            )
        else:
            bind_arguments.setdefault("clause", statement)
            execution_options = execution_options.union(
                {"future_result": True}
            )

        if _parent_execute_state:
            events_todo = _parent_execute_state._remaining_events()
        else:
            events_todo = self.dispatch.do_orm_execute
            if _add_event:
                events_todo = list(events_todo) + [_add_event]

        if events_todo:
            orm_exec_state = ORMExecuteState(
                self,
                statement,
                params,
                execution_options,
                bind_arguments,
                compile_state_cls,
                events_todo,
            )
            for idx, fn in enumerate(events_todo):
                orm_exec_state._starting_event_idx = idx
                result = fn(orm_exec_state)
                if result:
                    return result

            statement = orm_exec_state.statement
            execution_options = orm_exec_state.local_execution_options

        bind = self.get_bind(**bind_arguments)

        if self.autocommit:
            # legacy stuff, we can't use future_result w/ autocommit because
            # we rely upon close_with_result, also legacy.  it's all
            # interrelated
            conn = self._connection_for_bind(bind, close_with_result=True)
            execution_options = execution_options.union(
                dict(future_result=False)
            )
        else:
            conn = self._connection_for_bind(bind)
        result = conn._execute_20(statement, params or {}, execution_options)

        if compile_state_cls:
            result = compile_state_cls.orm_setup_cursor_result(
                self,
                statement,
                params,
                execution_options,
                bind_arguments,
                result,
            )

        return result

    def scalar(
        self,
        statement,
        params=None,
        execution_options=util.EMPTY_DICT,
        bind_arguments=None,
        **kw
    ):
        """Execute a statement and return a scalar result.

        Usage and parameters are the same as that of
        :meth:`_orm.Session.execute`; the return result is a scalar Python
        value.

        """

        return self.execute(
            statement,
            params=params,
            execution_options=execution_options,
            bind_arguments=bind_arguments,
            **kw
        ).scalar()

    def close(self):
        """Close out the transactional resources and ORM objects used by this
        :class:`_orm.Session`.

        This expunges all ORM objects associated with this
        :class:`_orm.Session`, ends any transaction in progress and
        :term:`releases` any :class:`_engine.Connection` objects which this
        :class:`_orm.Session` itself has checked out from associated
        :class:`_engine.Engine` objects. The operation then leaves the
        :class:`_orm.Session` in a state which it may be used again.

        .. tip::

            The :meth:`_orm.Session.close` method **does not prevent the
            Session from being used again**.   The :class:`_orm.Session` itself
            does not actually have a distinct "closed" state; it merely means
            the :class:`_orm.Session` will release all database connections
            and ORM objects.

        .. versionchanged:: 1.4  The :meth:`.Session.close` method does not
           immediately create a new :class:`.SessionTransaction` object;
           instead, the new :class:`.SessionTransaction` is created only if
           the :class:`.Session` is used again for a database operation.

        .. seealso::

            :ref:`session_closing` - detail on the semantics of
            :meth:`_orm.Session.close`

        """
        self._close_impl(invalidate=False)

    def invalidate(self):
        """Close this Session, using connection invalidation.

        This is a variant of :meth:`.Session.close` that will additionally
        ensure that the :meth:`_engine.Connection.invalidate`
        method will be called on each :class:`_engine.Connection` object
        that is currently in use for a transaction (typically there is only
        one connection unless the :class:`_orm.Session` is used with
        multiple engines).

        This can be called when the database is known to be in a state where
        the connections are no longer safe to be used.

        Below illustrates a scenario when using `gevent
        <https://www.gevent.org/>`_, which can produce ``Timeout`` exceptions
        that may mean the underlying connection should be discarded::

            import gevent

            try:
                sess = Session()
                sess.add(User())
                sess.commit()
            except gevent.Timeout:
                sess.invalidate()
                raise
            except:
                sess.rollback()
                raise

        The method additionally does everything that :meth:`_orm.Session.close`
        does, including that all ORM objects are expunged.

        """
        self._close_impl(invalidate=True)

    def _close_impl(self, invalidate):
        self.expunge_all()
        if self._transaction is not None:
            for transaction in self._transaction._iterate_self_and_parents():
                transaction.close(invalidate)

    def expunge_all(self):
        """Remove all object instances from this ``Session``.

        This is equivalent to calling ``expunge(obj)`` on all objects in this
        ``Session``.

        """

        all_states = self.identity_map.all_states() + list(self._new)
        self.identity_map = identity.WeakInstanceDict()
        self._new = {}
        self._deleted = {}

        statelib.InstanceState._detach_states(all_states, self)

    def _add_bind(self, key, bind):
        try:
            insp = inspect(key)
        except sa_exc.NoInspectionAvailable as err:
            if not isinstance(key, type):
                util.raise_(
                    sa_exc.ArgumentError(
                        "Not an acceptable bind target: %s" % key
                    ),
                    replace_context=err,
                )
            else:
                self.__binds[key] = bind
        else:
            if insp.is_selectable:
                self.__binds[insp] = bind
            elif insp.is_mapper:
                self.__binds[insp.class_] = bind
                for _selectable in insp._all_tables:
                    self.__binds[_selectable] = bind
            else:
                raise sa_exc.ArgumentError(
                    "Not an acceptable bind target: %s" % key
                )

    def bind_mapper(self, mapper, bind):
        """Associate a :class:`_orm.Mapper` or arbitrary Python class with a
        "bind", e.g. an :class:`_engine.Engine` or
        :class:`_engine.Connection`.

        The given entity is added to a lookup used by the
        :meth:`.Session.get_bind` method.

        :param mapper: a :class:`_orm.Mapper` object,
         or an instance of a mapped
         class, or any Python class that is the base of a set of mapped
         classes.

        :param bind: an :class:`_engine.Engine` or :class:`_engine.Connection`
                    object.

        .. seealso::

            :ref:`session_partitioning`

            :paramref:`.Session.binds`

            :meth:`.Session.bind_table`


        """
        self._add_bind(mapper, bind)

    def bind_table(self, table, bind):
        """Associate a :class:`_schema.Table` with a "bind", e.g. an
        :class:`_engine.Engine`
        or :class:`_engine.Connection`.

        The given :class:`_schema.Table` is added to a lookup used by the
        :meth:`.Session.get_bind` method.

        :param table: a :class:`_schema.Table` object,
         which is typically the target
         of an ORM mapping, or is present within a selectable that is
         mapped.

        :param bind: an :class:`_engine.Engine` or :class:`_engine.Connection`
                    object.

        .. seealso::

            :ref:`session_partitioning`

            :paramref:`.Session.binds`

            :meth:`.Session.bind_mapper`


        """
        self._add_bind(table, bind)

    def get_bind(
        self,
        mapper=None,
        clause=None,
        bind=None,
        _sa_skip_events=None,
        _sa_skip_for_implicit_returning=False,
    ):
        """Return a "bind" to which this :class:`.Session` is bound.

        The "bind" is usually an instance of :class:`_engine.Engine`,
        except in the case where the :class:`.Session` has been
        explicitly bound directly to a :class:`_engine.Connection`.

        For a multiply-bound or unbound :class:`.Session`, the
        ``mapper`` or ``clause`` arguments are used to determine the
        appropriate bind to return.

        Note that the "mapper" argument is usually present
        when :meth:`.Session.get_bind` is called via an ORM
        operation such as a :meth:`.Session.query`, each
        individual INSERT/UPDATE/DELETE operation within a
        :meth:`.Session.flush`, call, etc.

        The order of resolution is:

        1. if mapper given and :paramref:`.Session.binds` is present,
           locate a bind based first on the mapper in use, then
           on the mapped class in use, then on any base classes that are
           present in the ``__mro__`` of the mapped class, from more specific
           superclasses to more general.
        2. if clause given and ``Session.binds`` is present,
           locate a bind based on :class:`_schema.Table` objects
           found in the given clause present in ``Session.binds``.
        3. if ``Session.binds`` is present, return that.
        4. if clause given, attempt to return a bind
           linked to the :class:`_schema.MetaData` ultimately
           associated with the clause.
        5. if mapper given, attempt to return a bind
           linked to the :class:`_schema.MetaData` ultimately
           associated with the :class:`_schema.Table` or other
           selectable to which the mapper is mapped.
        6. No bind can be found, :exc:`~sqlalchemy.exc.UnboundExecutionError`
           is raised.

        Note that the :meth:`.Session.get_bind` method can be overridden on
        a user-defined subclass of :class:`.Session` to provide any kind
        of bind resolution scheme.  See the example at
        :ref:`session_custom_partitioning`.

        :param mapper:
          Optional :func:`.mapper` mapped class or instance of
          :class:`_orm.Mapper`.   The bind can be derived from a
          :class:`_orm.Mapper`
          first by consulting the "binds" map associated with this
          :class:`.Session`, and secondly by consulting the
          :class:`_schema.MetaData`
          associated with the :class:`_schema.Table` to which the
          :class:`_orm.Mapper`
          is mapped for a bind.

        :param clause:
            A :class:`_expression.ClauseElement` (i.e.
            :func:`_expression.select`,
            :func:`_expression.text`,
            etc.).  If the ``mapper`` argument is not present or could not
            produce a bind, the given expression construct will be searched
            for a bound element, typically a :class:`_schema.Table`
            associated with
            bound :class:`_schema.MetaData`.

        .. seealso::

             :ref:`session_partitioning`

             :paramref:`.Session.binds`

             :meth:`.Session.bind_mapper`

             :meth:`.Session.bind_table`

        """

        # this function is documented as a subclassing hook, so we have
        # to call this method even if the return is simple
        if bind:
            return bind
        elif not self.__binds and self.bind:
            # simplest and most common case, we have a bind and no
            # per-mapper/table binds, we're done
            return self.bind

        # we don't have self.bind and either have self.__binds
        # or we don't have self.__binds (which is legacy).  Look at the
        # mapper and the clause
        if mapper is clause is None:
            if self.bind:
                return self.bind
            else:
                raise sa_exc.UnboundExecutionError(
                    "This session is not bound to a single Engine or "
                    "Connection, and no context was provided to locate "
                    "a binding."
                )

        # look more closely at the mapper.
        if mapper is not None:
            try:
                mapper = inspect(mapper)
            except sa_exc.NoInspectionAvailable as err:
                if isinstance(mapper, type):
                    util.raise_(
                        exc.UnmappedClassError(mapper),
                        replace_context=err,
                    )
                else:
                    raise

        # match up the mapper or clause in the __binds
        if self.__binds:
            # matching mappers and selectables to entries in the
            # binds dictionary; supported use case.
            if mapper:
                for cls in mapper.class_.__mro__:
                    if cls in self.__binds:
                        return self.__binds[cls]
                if clause is None:
                    clause = mapper.persist_selectable

            if clause is not None:
                plugin_subject = clause._propagate_attrs.get(
                    "plugin_subject", None
                )

                if plugin_subject is not None:
                    for cls in plugin_subject.mapper.class_.__mro__:
                        if cls in self.__binds:
                            return self.__binds[cls]

                for obj in visitors.iterate(clause):
                    if obj in self.__binds:
                        return self.__binds[obj]

        # none of the __binds matched, but we have a fallback bind.
        # return that
        if self.bind:
            return self.bind

        # now we are in legacy territory.  looking for "bind" on tables
        # that are via bound metadata.   this goes away in 2.0.

        future_msg = ""
        future_code = ""

        if mapper and clause is None:
            clause = mapper.persist_selectable

        if clause is not None:
            if clause.bind:
                if self.future:
                    future_msg = (
                        " A bind was located via legacy bound metadata, but "
                        "since future=True is set on this Session, this "
                        "bind is ignored."
                    )
                else:
                    util.warn_deprecated_20(
                        "This Session located a target engine via bound "
                        "metadata; as this functionality will be removed in "
                        "SQLAlchemy 2.0, an Engine object should be passed "
                        "to the Session() constructor directly."
                    )
                    return clause.bind

        if mapper:
            if mapper.persist_selectable.bind:
                if self.future:
                    future_msg = (
                        " A bind was located via legacy bound metadata, but "
                        "since future=True is set on this Session, this "
                        "bind is ignored."
                    )
                else:
                    util.warn_deprecated_20(
                        "This Session located a target engine via bound "
                        "metadata; as this functionality will be removed in "
                        "SQLAlchemy 2.0, an Engine object should be passed "
                        "to the Session() constructor directly."
                    )
                    return mapper.persist_selectable.bind

        context = []
        if mapper is not None:
            context.append("mapper %s" % mapper)
        if clause is not None:
            context.append("SQL expression")

        raise sa_exc.UnboundExecutionError(
            "Could not locate a bind configured on %s or this Session.%s"
            % (", ".join(context), future_msg),
            code=future_code,
        )

    def query(self, *entities, **kwargs):
        """Return a new :class:`_query.Query` object corresponding to this
        :class:`_orm.Session`.

        """

        return self._query_cls(entities, self, **kwargs)

    def _identity_lookup(
        self,
        mapper,
        primary_key_identity,
        identity_token=None,
        passive=attributes.PASSIVE_OFF,
        lazy_loaded_from=None,
    ):
        """Locate an object in the identity map.

        Given a primary key identity, constructs an identity key and then
        looks in the session's identity map.  If present, the object may
        be run through unexpiration rules (e.g. load unloaded attributes,
        check if was deleted).

        e.g.::

            obj = session._identity_lookup(inspect(SomeClass), (1, ))

        :param mapper: mapper in use
        :param primary_key_identity: the primary key we are searching for, as
         a tuple.
        :param identity_token: identity token that should be used to create
         the identity key.  Used as is, however overriding subclasses can
         repurpose this in order to interpret the value in a special way,
         such as if None then look among multiple target tokens.
        :param passive: passive load flag passed to
         :func:`.loading.get_from_identity`, which impacts the behavior if
         the object is found; the object may be validated and/or unexpired
         if the flag allows for SQL to be emitted.
        :param lazy_loaded_from: an :class:`.InstanceState` that is
         specifically asking for this identity as a related identity.  Used
         for sharding schemes where there is a correspondence between an object
         and a related object being lazy-loaded (or otherwise
         relationship-loaded).

        :return: None if the object is not found in the identity map, *or*
         if the object was unexpired and found to have been deleted.
         if passive flags disallow SQL and the object is expired, returns
         PASSIVE_NO_RESULT.   In all other cases the instance is returned.

        .. versionchanged:: 1.4.0 - the :meth:`.Session._identity_lookup`
           method was moved from :class:`_query.Query` to
           :class:`.Session`, to avoid having to instantiate the
           :class:`_query.Query` object.


        """

        key = mapper.identity_key_from_primary_key(
            primary_key_identity, identity_token=identity_token
        )
        return loading.get_from_identity(self, mapper, key, passive)

    @property
    @util.contextmanager
    def no_autoflush(self):
        """Return a context manager that disables autoflush.

        e.g.::

            with session.no_autoflush:

                some_object = SomeClass()
                session.add(some_object)
                # won't autoflush
                some_object.related_thing = session.query(SomeRelated).first()

        Operations that proceed within the ``with:`` block
        will not be subject to flushes occurring upon query
        access.  This is useful when initializing a series
        of objects which involve existing database queries,
        where the uncompleted object should not yet be flushed.

        """
        autoflush = self.autoflush
        self.autoflush = False
        try:
            yield self
        finally:
            self.autoflush = autoflush

    def _autoflush(self):
        if self.autoflush and not self._flushing:
            try:
                self.flush()
            except sa_exc.StatementError as e:
                # note we are reraising StatementError as opposed to
                # raising FlushError with "chaining" to remain compatible
                # with code that catches StatementError, IntegrityError,
                # etc.
                e.add_detail(
                    "raised as a result of Query-invoked autoflush; "
                    "consider using a session.no_autoflush block if this "
                    "flush is occurring prematurely"
                )
                util.raise_(e, with_traceback=sys.exc_info()[2])

    def refresh(self, instance, attribute_names=None, with_for_update=None):
        """Expire and refresh attributes on the given instance.

        The selected attributes will first be expired as they would when using
        :meth:`_orm.Session.expire`; then a SELECT statement will be issued to
        the database to refresh column-oriented attributes with the current
        value available in the current transaction.

        :func:`_orm.relationship` oriented attributes will also be immediately
        loaded if they were already eagerly loaded on the object, using the
        same eager loading strategy that they were loaded with originally.
        Unloaded relationship attributes will remain unloaded, as will
        relationship attributes that were originally lazy loaded.

        .. versionadded:: 1.4 - the :meth:`_orm.Session.refresh` method
           can also refresh eagerly loaded attributes.

        .. tip::

            While the :meth:`_orm.Session.refresh` method is capable of
            refreshing both column and relationship oriented attributes, its
            primary focus is on refreshing of local column-oriented attributes
            on a single instance. For more open ended "refresh" functionality,
            including the ability to refresh the attributes on many objects at
            once while having explicit control over relationship loader
            strategies, use the
            :ref:`populate existing <orm_queryguide_populate_existing>` feature
            instead.

        Note that a highly isolated transaction will return the same values as
        were previously read in that same transaction, regardless of changes
        in database state outside of that transaction.   Refreshing
        attributes usually only makes sense at the start of a transaction
        where database rows have not yet been accessed.

        :param attribute_names: optional.  An iterable collection of
          string attribute names indicating a subset of attributes to
          be refreshed.

        :param with_for_update: optional boolean ``True`` indicating FOR UPDATE
          should be used, or may be a dictionary containing flags to
          indicate a more specific set of FOR UPDATE flags for the SELECT;
          flags should match the parameters of
          :meth:`_query.Query.with_for_update`.
          Supersedes the :paramref:`.Session.refresh.lockmode` parameter.

        .. seealso::

            :ref:`session_expire` - introductory material

            :meth:`.Session.expire`

            :meth:`.Session.expire_all`

            :ref:`orm_queryguide_populate_existing` - allows any ORM query
            to refresh objects as they would be loaded normally.

        """
        try:
            state = attributes.instance_state(instance)
        except exc.NO_STATE as err:
            util.raise_(
                exc.UnmappedInstanceError(instance),
                replace_context=err,
            )

        self._expire_state(state, attribute_names)

        if with_for_update == {}:
            raise sa_exc.ArgumentError(
                "with_for_update should be the boolean value "
                "True, or a dictionary with options.  "
                "A blank dictionary is ambiguous."
            )

        with_for_update = query.ForUpdateArg._from_argument(with_for_update)

        stmt = sql.select(object_mapper(instance))
        if (
            loading.load_on_ident(
                self,
                stmt,
                state.key,
                refresh_state=state,
                with_for_update=with_for_update,
                only_load_props=attribute_names,
            )
            is None
        ):
            raise sa_exc.InvalidRequestError(
                "Could not refresh instance '%s'" % instance_str(instance)
            )

    def expire_all(self):
        """Expires all persistent instances within this Session.

        When any attributes on a persistent instance is next accessed,
        a query will be issued using the
        :class:`.Session` object's current transactional context in order to
        load all expired attributes for the given instance.   Note that
        a highly isolated transaction will return the same values as were
        previously read in that same transaction, regardless of changes
        in database state outside of that transaction.

        To expire individual objects and individual attributes
        on those objects, use :meth:`Session.expire`.

        The :class:`.Session` object's default behavior is to
        expire all state whenever the :meth:`Session.rollback`
        or :meth:`Session.commit` methods are called, so that new
        state can be loaded for the new transaction.   For this reason,
        calling :meth:`Session.expire_all` should not be needed when
        autocommit is ``False``, assuming the transaction is isolated.

        .. seealso::

            :ref:`session_expire` - introductory material

            :meth:`.Session.expire`

            :meth:`.Session.refresh`

            :meth:`_orm.Query.populate_existing`

        """
        for state in self.identity_map.all_states():
            state._expire(state.dict, self.identity_map._modified)

    def expire(self, instance, attribute_names=None):
        """Expire the attributes on an instance.

        Marks the attributes of an instance as out of date. When an expired
        attribute is next accessed, a query will be issued to the
        :class:`.Session` object's current transactional context in order to
        load all expired attributes for the given instance.   Note that
        a highly isolated transaction will return the same values as were
        previously read in that same transaction, regardless of changes
        in database state outside of that transaction.

        To expire all objects in the :class:`.Session` simultaneously,
        use :meth:`Session.expire_all`.

        The :class:`.Session` object's default behavior is to
        expire all state whenever the :meth:`Session.rollback`
        or :meth:`Session.commit` methods are called, so that new
        state can be loaded for the new transaction.   For this reason,
        calling :meth:`Session.expire` only makes sense for the specific
        case that a non-ORM SQL statement was emitted in the current
        transaction.

        :param instance: The instance to be refreshed.
        :param attribute_names: optional list of string attribute names
          indicating a subset of attributes to be expired.

        .. seealso::

            :ref:`session_expire` - introductory material

            :meth:`.Session.expire`

            :meth:`.Session.refresh`

            :meth:`_orm.Query.populate_existing`

        """
        try:
            state = attributes.instance_state(instance)
        except exc.NO_STATE as err:
            util.raise_(
                exc.UnmappedInstanceError(instance),
                replace_context=err,
            )
        self._expire_state(state, attribute_names)

    def _expire_state(self, state, attribute_names):
        self._validate_persistent(state)
        if attribute_names:
            state._expire_attributes(state.dict, attribute_names)
        else:
            # pre-fetch the full cascade since the expire is going to
            # remove associations
            cascaded = list(
                state.manager.mapper.cascade_iterator("refresh-expire", state)
            )
            self._conditional_expire(state)
            for o, m, st_, dct_ in cascaded:
                self._conditional_expire(st_)

    def _conditional_expire(self, state, autoflush=None):
        """Expire a state if persistent, else expunge if pending"""

        if state.key:
            state._expire(state.dict, self.identity_map._modified)
        elif state in self._new:
            self._new.pop(state)
            state._detach(self)

    def expunge(self, instance):
        """Remove the `instance` from this ``Session``.

        This will free all internal references to the instance.  Cascading
        will be applied according to the *expunge* cascade rule.

        """
        try:
            state = attributes.instance_state(instance)
        except exc.NO_STATE as err:
            util.raise_(
                exc.UnmappedInstanceError(instance),
                replace_context=err,
            )
        if state.session_id is not self.hash_key:
            raise sa_exc.InvalidRequestError(
                "Instance %s is not present in this Session" % state_str(state)
            )

        cascaded = list(
            state.manager.mapper.cascade_iterator("expunge", state)
        )
        self._expunge_states([state] + [st_ for o, m, st_, dct_ in cascaded])

    def _expunge_states(self, states, to_transient=False):
        for state in states:
            if state in self._new:
                self._new.pop(state)
            elif self.identity_map.contains_state(state):
                self.identity_map.safe_discard(state)
                self._deleted.pop(state, None)
            elif self._transaction:
                # state is "detached" from being deleted, but still present
                # in the transaction snapshot
                self._transaction._deleted.pop(state, None)
        statelib.InstanceState._detach_states(
            states, self, to_transient=to_transient
        )

    def _register_persistent(self, states):
        """Register all persistent objects from a flush.

        This is used both for pending objects moving to the persistent
        state as well as already persistent objects.

        """

        pending_to_persistent = self.dispatch.pending_to_persistent or None
        for state in states:
            mapper = _state_mapper(state)

            # prevent against last minute dereferences of the object
            obj = state.obj()
            if obj is not None:

                instance_key = mapper._identity_key_from_state(state)

                if (
                    _none_set.intersection(instance_key[1])
                    and not mapper.allow_partial_pks
                    or _none_set.issuperset(instance_key[1])
                ):
                    raise exc.FlushError(
                        "Instance %s has a NULL identity key.  If this is an "
                        "auto-generated value, check that the database table "
                        "allows generation of new primary key values, and "
                        "that the mapped Column object is configured to "
                        "expect these generated values.  Ensure also that "
                        "this flush() is not occurring at an inappropriate "
                        "time, such as within a load() event."
                        % state_str(state)
                    )

                if state.key is None:
                    state.key = instance_key
                elif state.key != instance_key:
                    # primary key switch. use safe_discard() in case another
                    # state has already replaced this one in the identity
                    # map (see test/orm/test_naturalpks.py ReversePKsTest)
                    self.identity_map.safe_discard(state)
                    if state in self._transaction._key_switches:
                        orig_key = self._transaction._key_switches[state][0]
                    else:
                        orig_key = state.key
                    self._transaction._key_switches[state] = (
                        orig_key,
                        instance_key,
                    )
                    state.key = instance_key

                # there can be an existing state in the identity map
                # that is replaced when the primary keys of two instances
                # are swapped; see test/orm/test_naturalpks.py -> test_reverse
                old = self.identity_map.replace(state)
                if (
                    old is not None
                    and mapper._identity_key_from_state(old) == instance_key
                    and old.obj() is not None
                ):
                    util.warn(
                        "Identity map already had an identity for %s, "
                        "replacing it with newly flushed object.   Are there "
                        "load operations occurring inside of an event handler "
                        "within the flush?" % (instance_key,)
                    )
                state._orphaned_outside_of_session = False

        statelib.InstanceState._commit_all_states(
            ((state, state.dict) for state in states), self.identity_map
        )

        self._register_altered(states)

        if pending_to_persistent is not None:
            for state in states.intersection(self._new):
                pending_to_persistent(self, state)

        # remove from new last, might be the last strong ref
        for state in set(states).intersection(self._new):
            self._new.pop(state)

    def _register_altered(self, states):
        if self._transaction:
            for state in states:
                if state in self._new:
                    self._transaction._new[state] = True
                else:
                    self._transaction._dirty[state] = True

    def _remove_newly_deleted(self, states):
        persistent_to_deleted = self.dispatch.persistent_to_deleted or None
        for state in states:
            if self._transaction:
                self._transaction._deleted[state] = True

            if persistent_to_deleted is not None:
                # get a strong reference before we pop out of
                # self._deleted
                obj = state.obj()  # noqa

            self.identity_map.safe_discard(state)
            self._deleted.pop(state, None)
            state._deleted = True
            # can't call state._detach() here, because this state
            # is still in the transaction snapshot and needs to be
            # tracked as part of that
            if persistent_to_deleted is not None:
                persistent_to_deleted(self, state)

    def add(self, instance, _warn=True):
        """Place an object in the ``Session``.

        Its state will be persisted to the database on the next flush
        operation.

        Repeated calls to ``add()`` will be ignored. The opposite of ``add()``
        is ``expunge()``.

        """
        if _warn and self._warn_on_events:
            self._flush_warning("Session.add()")

        try:
            state = attributes.instance_state(instance)
        except exc.NO_STATE as err:
            util.raise_(
                exc.UnmappedInstanceError(instance),
                replace_context=err,
            )

        self._save_or_update_state(state)

    def add_all(self, instances):
        """Add the given collection of instances to this ``Session``."""

        if self._warn_on_events:
            self._flush_warning("Session.add_all()")

        for instance in instances:
            self.add(instance, _warn=False)

    def _save_or_update_state(self, state):
        state._orphaned_outside_of_session = False
        self._save_or_update_impl(state)

        mapper = _state_mapper(state)
        for o, m, st_, dct_ in mapper.cascade_iterator(
            "save-update", state, halt_on=self._contains_state
        ):
            self._save_or_update_impl(st_)

    def delete(self, instance):
        """Mark an instance as deleted.

        The database delete operation occurs upon ``flush()``.

        """
        if self._warn_on_events:
            self._flush_warning("Session.delete()")

        try:
            state = attributes.instance_state(instance)
        except exc.NO_STATE as err:
            util.raise_(
                exc.UnmappedInstanceError(instance),
                replace_context=err,
            )

        self._delete_impl(state, instance, head=True)

    def _delete_impl(self, state, obj, head):

        if state.key is None:
            if head:
                raise sa_exc.InvalidRequestError(
                    "Instance '%s' is not persisted" % state_str(state)
                )
            else:
                return

        to_attach = self._before_attach(state, obj)

        if state in self._deleted:
            return

        self.identity_map.add(state)

        if to_attach:
            self._after_attach(state, obj)

        if head:
            # grab the cascades before adding the item to the deleted list
            # so that autoflush does not delete the item
            # the strong reference to the instance itself is significant here
            cascade_states = list(
                state.manager.mapper.cascade_iterator("delete", state)
            )

        self._deleted[state] = obj

        if head:
            for o, m, st_, dct_ in cascade_states:
                self._delete_impl(st_, o, False)

    def get(
        self,
        entity,
        ident,
        options=None,
        populate_existing=False,
        with_for_update=None,
        identity_token=None,
    ):
        """Return an instance based on the given primary key identifier,
        or ``None`` if not found.

        E.g.::

            my_user = session.get(User, 5)

            some_object = session.get(VersionedFoo, (5, 10))

            some_object = session.get(
                VersionedFoo,
                {"id": 5, "version_id": 10}
            )

        .. versionadded:: 1.4 Added :meth:`_orm.Session.get`, which is moved
           from the now deprecated :meth:`_orm.Query.get` method.

        :meth:`_orm.Session.get` is special in that it provides direct
        access to the identity map of the :class:`.Session`.
        If the given primary key identifier is present
        in the local identity map, the object is returned
        directly from this collection and no SQL is emitted,
        unless the object has been marked fully expired.
        If not present,
        a SELECT is performed in order to locate the object.

        :meth:`_orm.Session.get` also will perform a check if
        the object is present in the identity map and
        marked as expired - a SELECT
        is emitted to refresh the object as well as to
        ensure that the row is still present.
        If not, :class:`~sqlalchemy.orm.exc.ObjectDeletedError` is raised.

        :param entity: a mapped class or :class:`.Mapper` indicating the
         type of entity to be loaded.

        :param ident: A scalar, tuple, or dictionary representing the
         primary key.  For a composite (e.g. multiple column) primary key,
         a tuple or dictionary should be passed.

         For a single-column primary key, the scalar calling form is typically
         the most expedient.  If the primary key of a row is the value "5",
         the call looks like::

            my_object = session.get(SomeClass, 5)

         The tuple form contains primary key values typically in
         the order in which they correspond to the mapped
         :class:`_schema.Table`
         object's primary key columns, or if the
         :paramref:`_orm.Mapper.primary_key` configuration parameter were
         used, in
         the order used for that parameter. For example, if the primary key
         of a row is represented by the integer
         digits "5, 10" the call would look like::

             my_object = session.get(SomeClass, (5, 10))

         The dictionary form should include as keys the mapped attribute names
         corresponding to each element of the primary key.  If the mapped class
         has the attributes ``id``, ``version_id`` as the attributes which
         store the object's primary key value, the call would look like::

            my_object = session.get(SomeClass, {"id": 5, "version_id": 10})

        :param options: optional sequence of loader options which will be
         applied to the query, if one is emitted.

        :param populate_existing: causes the method to unconditionally emit
         a SQL query and refresh the object with the newly loaded data,
         regardless of whether or not the object is already present.

        :param with_for_update: optional boolean ``True`` indicating FOR UPDATE
          should be used, or may be a dictionary containing flags to
          indicate a more specific set of FOR UPDATE flags for the SELECT;
          flags should match the parameters of
          :meth:`_query.Query.with_for_update`.
          Supersedes the :paramref:`.Session.refresh.lockmode` parameter.

        :return: The object instance, or ``None``.

        """
        return self._get_impl(
            entity,
            ident,
            loading.load_on_pk_identity,
            options,
            populate_existing=populate_existing,
            with_for_update=with_for_update,
            identity_token=identity_token,
        )

    def _get_impl(
        self,
        entity,
        primary_key_identity,
        db_load_fn,
        options=None,
        populate_existing=False,
        with_for_update=None,
        identity_token=None,
        execution_options=None,
    ):

        # convert composite types to individual args
        if hasattr(primary_key_identity, "__composite_values__"):
            primary_key_identity = primary_key_identity.__composite_values__()

        mapper = inspect(entity)

        is_dict = isinstance(primary_key_identity, dict)
        if not is_dict:
            primary_key_identity = util.to_list(
                primary_key_identity, default=(None,)
            )

        if len(primary_key_identity) != len(mapper.primary_key):
            raise sa_exc.InvalidRequestError(
                "Incorrect number of values in identifier to formulate "
                "primary key for query.get(); primary key columns are %s"
                % ",".join("'%s'" % c for c in mapper.primary_key)
            )

        if is_dict:
            try:
                primary_key_identity = list(
                    primary_key_identity[prop.key]
                    for prop in mapper._identity_key_props
                )

            except KeyError as err:
                util.raise_(
                    sa_exc.InvalidRequestError(
                        "Incorrect names of values in identifier to formulate "
                        "primary key for query.get(); primary key attribute "
                        "names are %s"
                        % ",".join(
                            "'%s'" % prop.key
                            for prop in mapper._identity_key_props
                        )
                    ),
                    replace_context=err,
                )

        if (
            not populate_existing
            and not mapper.always_refresh
            and with_for_update is None
        ):

            instance = self._identity_lookup(
                mapper, primary_key_identity, identity_token=identity_token
            )

            if instance is not None:
                # reject calls for id in identity map but class
                # mismatch.
                if not issubclass(instance.__class__, mapper.class_):
                    return None
                return instance
            elif instance is attributes.PASSIVE_CLASS_MISMATCH:
                return None

        # set_label_style() not strictly necessary, however this will ensure
        # that tablename_colname style is used which at the moment is
        # asserted in a lot of unit tests :)

        load_options = context.QueryContext.default_load_options

        if populate_existing:
            load_options += {"_populate_existing": populate_existing}
        statement = sql.select(mapper).set_label_style(
            LABEL_STYLE_TABLENAME_PLUS_COL
        )
        if with_for_update is not None:
            statement._for_update_arg = query.ForUpdateArg._from_argument(
                with_for_update
            )

        if options:
            statement = statement.options(*options)
        if execution_options:
            statement = statement.execution_options(**execution_options)
        return db_load_fn(
            self,
            statement,
            primary_key_identity,
            load_options=load_options,
        )

    def merge(self, instance, load=True, options=None):
        """Copy the state of a given instance into a corresponding instance
        within this :class:`.Session`.

        :meth:`.Session.merge` examines the primary key attributes of the
        source instance, and attempts to reconcile it with an instance of the
        same primary key in the session.   If not found locally, it attempts
        to load the object from the database based on primary key, and if
        none can be located, creates a new instance.  The state of each
        attribute on the source instance is then copied to the target
        instance.  The resulting target instance is then returned by the
        method; the original source instance is left unmodified, and
        un-associated with the :class:`.Session` if not already.

        This operation cascades to associated instances if the association is
        mapped with ``cascade="merge"``.

        See :ref:`unitofwork_merging` for a detailed discussion of merging.

        .. versionchanged:: 1.1 - :meth:`.Session.merge` will now reconcile
           pending objects with overlapping primary keys in the same way
           as persistent.  See :ref:`change_3601` for discussion.

        :param instance: Instance to be merged.
        :param load: Boolean, when False, :meth:`.merge` switches into
         a "high performance" mode which causes it to forego emitting history
         events as well as all database access.  This flag is used for
         cases such as transferring graphs of objects into a :class:`.Session`
         from a second level cache, or to transfer just-loaded objects
         into the :class:`.Session` owned by a worker thread or process
         without re-querying the database.

         The ``load=False`` use case adds the caveat that the given
         object has to be in a "clean" state, that is, has no pending changes
         to be flushed - even if the incoming object is detached from any
         :class:`.Session`.   This is so that when
         the merge operation populates local attributes and
         cascades to related objects and
         collections, the values can be "stamped" onto the
         target object as is, without generating any history or attribute
         events, and without the need to reconcile the incoming data with
         any existing related objects or collections that might not
         be loaded.  The resulting objects from ``load=False`` are always
         produced as "clean", so it is only appropriate that the given objects
         should be "clean" as well, else this suggests a mis-use of the
         method.
        :param options: optional sequence of loader options which will be
         applied to the :meth:`_orm.Session.get` method when the merge
         operation loads the existing version of the object from the database.

         .. versionadded:: 1.4.24


        .. seealso::

            :func:`.make_transient_to_detached` - provides for an alternative
            means of "merging" a single object into the :class:`.Session`

        """

        if self._warn_on_events:
            self._flush_warning("Session.merge()")

        _recursive = {}
        _resolve_conflict_map = {}

        if load:
            # flush current contents if we expect to load data
            self._autoflush()

        object_mapper(instance)  # verify mapped
        autoflush = self.autoflush
        try:
            self.autoflush = False
            return self._merge(
                attributes.instance_state(instance),
                attributes.instance_dict(instance),
                load=load,
                options=options,
                _recursive=_recursive,
                _resolve_conflict_map=_resolve_conflict_map,
            )
        finally:
            self.autoflush = autoflush

    def _merge(
        self,
        state,
        state_dict,
        load=True,
        options=None,
        _recursive=None,
        _resolve_conflict_map=None,
    ):
        mapper = _state_mapper(state)
        if state in _recursive:
            return _recursive[state]

        new_instance = False
        key = state.key

        if key is None:
            if state in self._new:
                util.warn(
                    "Instance %s is already pending in this Session yet is "
                    "being merged again; this is probably not what you want "
                    "to do" % state_str(state)
                )

            if not load:
                raise sa_exc.InvalidRequestError(
                    "merge() with load=False option does not support "
                    "objects transient (i.e. unpersisted) objects.  flush() "
                    "all changes on mapped instances before merging with "
                    "load=False."
                )
            key = mapper._identity_key_from_state(state)
            key_is_persistent = attributes.NEVER_SET not in key[1] and (
                not _none_set.intersection(key[1])
                or (
                    mapper.allow_partial_pks
                    and not _none_set.issuperset(key[1])
                )
            )
        else:
            key_is_persistent = True

        if key in self.identity_map:
            try:
                merged = self.identity_map[key]
            except KeyError:
                # object was GC'ed right as we checked for it
                merged = None
        else:
            merged = None

        if merged is None:
            if key_is_persistent and key in _resolve_conflict_map:
                merged = _resolve_conflict_map[key]

            elif not load:
                if state.modified:
                    raise sa_exc.InvalidRequestError(
                        "merge() with load=False option does not support "
                        "objects marked as 'dirty'.  flush() all changes on "
                        "mapped instances before merging with load=False."
                    )
                merged = mapper.class_manager.new_instance()
                merged_state = attributes.instance_state(merged)
                merged_state.key = key
                self._update_impl(merged_state)
                new_instance = True

            elif key_is_persistent:
                merged = self.get(
                    mapper.class_,
                    key[1],
                    identity_token=key[2],
                    options=options,
                )

        if merged is None:
            merged = mapper.class_manager.new_instance()
            merged_state = attributes.instance_state(merged)
            merged_dict = attributes.instance_dict(merged)
            new_instance = True
            self._save_or_update_state(merged_state)
        else:
            merged_state = attributes.instance_state(merged)
            merged_dict = attributes.instance_dict(merged)

        _recursive[state] = merged
        _resolve_conflict_map[key] = merged

        # check that we didn't just pull the exact same
        # state out.
        if state is not merged_state:
            # version check if applicable
            if mapper.version_id_col is not None:
                existing_version = mapper._get_state_attr_by_column(
                    state,
                    state_dict,
                    mapper.version_id_col,
                    passive=attributes.PASSIVE_NO_INITIALIZE,
                )

                merged_version = mapper._get_state_attr_by_column(
                    merged_state,
                    merged_dict,
                    mapper.version_id_col,
                    passive=attributes.PASSIVE_NO_INITIALIZE,
                )

                if (
                    existing_version is not attributes.PASSIVE_NO_RESULT
                    and merged_version is not attributes.PASSIVE_NO_RESULT
                    and existing_version != merged_version
                ):
                    raise exc.StaleDataError(
                        "Version id '%s' on merged state %s "
                        "does not match existing version '%s'. "
                        "Leave the version attribute unset when "
                        "merging to update the most recent version."
                        % (
                            existing_version,
                            state_str(merged_state),
                            merged_version,
                        )
                    )

            merged_state.load_path = state.load_path
            merged_state.load_options = state.load_options

            # since we are copying load_options, we need to copy
            # the callables_ that would have been generated by those
            # load_options.
            # assumes that the callables we put in state.callables_
            # are not instance-specific (which they should not be)
            merged_state._copy_callables(state)

            for prop in mapper.iterate_properties:
                prop.merge(
                    self,
                    state,
                    state_dict,
                    merged_state,
                    merged_dict,
                    load,
                    _recursive,
                    _resolve_conflict_map,
                )

        if not load:
            # remove any history
            merged_state._commit_all(merged_dict, self.identity_map)

        if new_instance:
            merged_state.manager.dispatch.load(merged_state, None)
        return merged

    def _validate_persistent(self, state):
        if not self.identity_map.contains_state(state):
            raise sa_exc.InvalidRequestError(
                "Instance '%s' is not persistent within this Session"
                % state_str(state)
            )

    def _save_impl(self, state):
        if state.key is not None:
            raise sa_exc.InvalidRequestError(
                "Object '%s' already has an identity - "
                "it can't be registered as pending" % state_str(state)
            )

        obj = state.obj()
        to_attach = self._before_attach(state, obj)
        if state not in self._new:
            self._new[state] = obj
            state.insert_order = len(self._new)
        if to_attach:
            self._after_attach(state, obj)

    def _update_impl(self, state, revert_deletion=False):
        if state.key is None:
            raise sa_exc.InvalidRequestError(
                "Instance '%s' is not persisted" % state_str(state)
            )

        if state._deleted:
            if revert_deletion:
                if not state._attached:
                    return
                del state._deleted
            else:
                raise sa_exc.InvalidRequestError(
                    "Instance '%s' has been deleted.  "
                    "Use the make_transient() "
                    "function to send this object back "
                    "to the transient state." % state_str(state)
                )

        obj = state.obj()

        # check for late gc
        if obj is None:
            return

        to_attach = self._before_attach(state, obj)

        self._deleted.pop(state, None)
        if revert_deletion:
            self.identity_map.replace(state)
        else:
            self.identity_map.add(state)

        if to_attach:
            self._after_attach(state, obj)
        elif revert_deletion:
            self.dispatch.deleted_to_persistent(self, state)

    def _save_or_update_impl(self, state):
        if state.key is None:
            self._save_impl(state)
        else:
            self._update_impl(state)

    def enable_relationship_loading(self, obj):
        """Associate an object with this :class:`.Session` for related
        object loading.

        .. warning::

            :meth:`.enable_relationship_loading` exists to serve special
            use cases and is not recommended for general use.

        Accesses of attributes mapped with :func:`_orm.relationship`
        will attempt to load a value from the database using this
        :class:`.Session` as the source of connectivity.  The values
        will be loaded based on foreign key and primary key values
        present on this object - if not present, then those relationships
        will be unavailable.

        The object will be attached to this session, but will
        **not** participate in any persistence operations; its state
        for almost all purposes will remain either "transient" or
        "detached", except for the case of relationship loading.

        Also note that backrefs will often not work as expected.
        Altering a relationship-bound attribute on the target object
        may not fire off a backref event, if the effective value
        is what was already loaded from a foreign-key-holding value.

        The :meth:`.Session.enable_relationship_loading` method is
        similar to the ``load_on_pending`` flag on :func:`_orm.relationship`.
        Unlike that flag, :meth:`.Session.enable_relationship_loading` allows
        an object to remain transient while still being able to load
        related items.

        To make a transient object associated with a :class:`.Session`
        via :meth:`.Session.enable_relationship_loading` pending, add
        it to the :class:`.Session` using :meth:`.Session.add` normally.
        If the object instead represents an existing identity in the database,
        it should be merged using :meth:`.Session.merge`.

        :meth:`.Session.enable_relationship_loading` does not improve
        behavior when the ORM is used normally - object references should be
        constructed at the object level, not at the foreign key level, so
        that they are present in an ordinary way before flush()
        proceeds.  This method is not intended for general use.

        .. seealso::

            :paramref:`_orm.relationship.load_on_pending` - this flag
            allows per-relationship loading of many-to-ones on items that
            are pending.

            :func:`.make_transient_to_detached` - allows for an object to
            be added to a :class:`.Session` without SQL emitted, which then
            will unexpire attributes on access.

        """
        try:
            state = attributes.instance_state(obj)
        except exc.NO_STATE as err:
            util.raise_(
                exc.UnmappedInstanceError(obj),
                replace_context=err,
            )

        to_attach = self._before_attach(state, obj)
        state._load_pending = True
        if to_attach:
            self._after_attach(state, obj)

    def _before_attach(self, state, obj):
        self._autobegin()

        if state.session_id == self.hash_key:
            return False

        if state.session_id and state.session_id in _sessions:
            raise sa_exc.InvalidRequestError(
                "Object '%s' is already attached to session '%s' "
                "(this is '%s')"
                % (state_str(state), state.session_id, self.hash_key)
            )

        self.dispatch.before_attach(self, state)

        return True

    def _after_attach(self, state, obj):
        state.session_id = self.hash_key
        if state.modified and state._strong_obj is None:
            state._strong_obj = obj
        self.dispatch.after_attach(self, state)

        if state.key:
            self.dispatch.detached_to_persistent(self, state)
        else:
            self.dispatch.transient_to_pending(self, state)

    def __contains__(self, instance):
        """Return True if the instance is associated with this session.

        The instance may be pending or persistent within the Session for a
        result of True.

        """
        try:
            state = attributes.instance_state(instance)
        except exc.NO_STATE as err:
            util.raise_(
                exc.UnmappedInstanceError(instance),
                replace_context=err,
            )
        return self._contains_state(state)

    def __iter__(self):
        """Iterate over all pending or persistent instances within this
        Session.

        """
        return iter(
            list(self._new.values()) + list(self.identity_map.values())
        )

    def _contains_state(self, state):
        return state in self._new or self.identity_map.contains_state(state)

    def flush(self, objects=None):
        """Flush all the object changes to the database.

        Writes out all pending object creations, deletions and modifications
        to the database as INSERTs, DELETEs, UPDATEs, etc.  Operations are
        automatically ordered by the Session's unit of work dependency
        solver.

        Database operations will be issued in the current transactional
        context and do not affect the state of the transaction, unless an
        error occurs, in which case the entire transaction is rolled back.
        You may flush() as often as you like within a transaction to move
        changes from Python to the database's transaction buffer.

        For ``autocommit`` Sessions with no active manual transaction, flush()
        will create a transaction on the fly that surrounds the entire set of
        operations into the flush.

        :param objects: Optional; restricts the flush operation to operate
          only on elements that are in the given collection.

          This feature is for an extremely narrow set of use cases where
          particular objects may need to be operated upon before the
          full flush() occurs.  It is not intended for general use.

        """

        if self._flushing:
            raise sa_exc.InvalidRequestError("Session is already flushing")

        if self._is_clean():
            return
        try:
            self._flushing = True
            self._flush(objects)
        finally:
            self._flushing = False

    def _flush_warning(self, method):
        util.warn(
            "Usage of the '%s' operation is not currently supported "
            "within the execution stage of the flush process. "
            "Results may not be consistent.  Consider using alternative "
            "event listeners or connection-level operations instead." % method
        )

    def _is_clean(self):
        return (
            not self.identity_map.check_modified()
            and not self._deleted
            and not self._new
        )

    def _flush(self, objects=None):

        dirty = self._dirty_states
        if not dirty and not self._deleted and not self._new:
            self.identity_map._modified.clear()
            return

        flush_context = UOWTransaction(self)

        if self.dispatch.before_flush:
            self.dispatch.before_flush(self, flush_context, objects)
            # re-establish "dirty states" in case the listeners
            # added
            dirty = self._dirty_states

        deleted = set(self._deleted)
        new = set(self._new)

        dirty = set(dirty).difference(deleted)

        # create the set of all objects we want to operate upon
        if objects:
            # specific list passed in
            objset = set()
            for o in objects:
                try:
                    state = attributes.instance_state(o)

                except exc.NO_STATE as err:
                    util.raise_(
                        exc.UnmappedInstanceError(o),
                        replace_context=err,
                    )
                objset.add(state)
        else:
            objset = None

        # store objects whose fate has been decided
        processed = set()

        # put all saves/updates into the flush context.  detect top-level
        # orphans and throw them into deleted.
        if objset:
            proc = new.union(dirty).intersection(objset).difference(deleted)
        else:
            proc = new.union(dirty).difference(deleted)

        for state in proc:
            is_orphan = _state_mapper(state)._is_orphan(state)

            is_persistent_orphan = is_orphan and state.has_identity

            if (
                is_orphan
                and not is_persistent_orphan
                and state._orphaned_outside_of_session
            ):
                self._expunge_states([state])
            else:
                _reg = flush_context.register_object(
                    state, isdelete=is_persistent_orphan
                )
                assert _reg, "Failed to add object to the flush context!"
                processed.add(state)

        # put all remaining deletes into the flush context.
        if objset:
            proc = deleted.intersection(objset).difference(processed)
        else:
            proc = deleted.difference(processed)
        for state in proc:
            _reg = flush_context.register_object(state, isdelete=True)
            assert _reg, "Failed to add object to the flush context!"

        if not flush_context.has_work:
            return

        flush_context.transaction = transaction = self.begin(_subtrans=True)
        try:
            self._warn_on_events = True
            try:
                flush_context.execute()
            finally:
                self._warn_on_events = False

            self.dispatch.after_flush(self, flush_context)

            flush_context.finalize_flush_changes()

            if not objects and self.identity_map._modified:
                len_ = len(self.identity_map._modified)

                statelib.InstanceState._commit_all_states(
                    [
                        (state, state.dict)
                        for state in self.identity_map._modified
                    ],
                    instance_dict=self.identity_map,
                )
                util.warn(
                    "Attribute history events accumulated on %d "
                    "previously clean instances "
                    "within inner-flush event handlers have been "
                    "reset, and will not result in database updates. "
                    "Consider using set_committed_value() within "
                    "inner-flush event handlers to avoid this warning." % len_
                )

            # useful assertions:
            # if not objects:
            #    assert not self.identity_map._modified
            # else:
            #    assert self.identity_map._modified == \
            #            self.identity_map._modified.difference(objects)

            self.dispatch.after_flush_postexec(self, flush_context)

            transaction.commit()

        except:
            with util.safe_reraise():
                transaction.rollback(_capture_exception=True)

    def bulk_save_objects(
        self,
        objects,
        return_defaults=False,
        update_changed_only=True,
        preserve_order=True,
    ):
        """Perform a bulk save of the given list of objects.

        The bulk save feature allows mapped objects to be used as the
        source of simple INSERT and UPDATE operations which can be more easily
        grouped together into higher performing "executemany"
        operations; the extraction of data from the objects is also performed
        using a lower-latency process that ignores whether or not attributes
        have actually been modified in the case of UPDATEs, and also ignores
        SQL expressions.

        The objects as given are not added to the session and no additional
        state is established on them, unless the ``return_defaults`` flag
        is also set, in which case primary key attributes and server-side
        default values will be populated.

        .. versionadded:: 1.0.0

        .. warning::

            The bulk save feature allows for a lower-latency INSERT/UPDATE
            of rows at the expense of most other unit-of-work features.
            Features such as object management, relationship handling,
            and SQL clause support are **silently omitted** in favor of raw
            INSERT/UPDATES of records.

            **Please read the list of caveats at**
            :ref:`bulk_operations_caveats` **before using this method, and
            fully test and confirm the functionality of all code developed
            using these systems.**

        :param objects: a sequence of mapped object instances.  The mapped
         objects are persisted as is, and are **not** associated with the
         :class:`.Session` afterwards.

         For each object, whether the object is sent as an INSERT or an
         UPDATE is dependent on the same rules used by the :class:`.Session`
         in traditional operation; if the object has the
         :attr:`.InstanceState.key`
         attribute set, then the object is assumed to be "detached" and
         will result in an UPDATE.  Otherwise, an INSERT is used.

         In the case of an UPDATE, statements are grouped based on which
         attributes have changed, and are thus to be the subject of each
         SET clause.  If ``update_changed_only`` is False, then all
         attributes present within each object are applied to the UPDATE
         statement, which may help in allowing the statements to be grouped
         together into a larger executemany(), and will also reduce the
         overhead of checking history on attributes.

        :param return_defaults: when True, rows that are missing values which
         generate defaults, namely integer primary key defaults and sequences,
         will be inserted **one at a time**, so that the primary key value
         is available.  In particular this will allow joined-inheritance
         and other multi-table mappings to insert correctly without the need
         to provide primary key values ahead of time; however,
         :paramref:`.Session.bulk_save_objects.return_defaults` **greatly
         reduces the performance gains** of the method overall.

        :param update_changed_only: when True, UPDATE statements are rendered
         based on those attributes in each state that have logged changes.
         When False, all attributes present are rendered into the SET clause
         with the exception of primary key attributes.

        :param preserve_order: when True, the order of inserts and updates
         matches exactly the order in which the objects are given.   When
         False, common types of objects are grouped into inserts
         and updates, to allow for more batching opportunities.

         .. versionadded:: 1.3

        .. seealso::

            :ref:`bulk_operations`

            :meth:`.Session.bulk_insert_mappings`

            :meth:`.Session.bulk_update_mappings`

        """

        def key(state):
            return (state.mapper, state.key is not None)

        obj_states = (attributes.instance_state(obj) for obj in objects)
        if not preserve_order:
            obj_states = sorted(obj_states, key=key)

        for (mapper, isupdate), states in itertools.groupby(obj_states, key):
            self._bulk_save_mappings(
                mapper,
                states,
                isupdate,
                True,
                return_defaults,
                update_changed_only,
                False,
            )

    def bulk_insert_mappings(
        self, mapper, mappings, return_defaults=False, render_nulls=False
    ):
        """Perform a bulk insert of the given list of mapping dictionaries.

        The bulk insert feature allows plain Python dictionaries to be used as
        the source of simple INSERT operations which can be more easily
        grouped together into higher performing "executemany"
        operations.  Using dictionaries, there is no "history" or session
        state management features in use, reducing latency when inserting
        large numbers of simple rows.

        The values within the dictionaries as given are typically passed
        without modification into Core :meth:`_expression.Insert` constructs,
        after
        organizing the values within them across the tables to which
        the given mapper is mapped.

        .. versionadded:: 1.0.0

        .. warning::

            The bulk insert feature allows for a lower-latency INSERT
            of rows at the expense of most other unit-of-work features.
            Features such as object management, relationship handling,
            and SQL clause support are **silently omitted** in favor of raw
            INSERT of records.

            **Please read the list of caveats at**
            :ref:`bulk_operations_caveats` **before using this method, and
            fully test and confirm the functionality of all code developed
            using these systems.**

        :param mapper: a mapped class, or the actual :class:`_orm.Mapper`
         object,
         representing the single kind of object represented within the mapping
         list.

        :param mappings: a sequence of dictionaries, each one containing the
         state of the mapped row to be inserted, in terms of the attribute
         names on the mapped class.   If the mapping refers to multiple tables,
         such as a joined-inheritance mapping, each dictionary must contain all
         keys to be populated into all tables.

        :param return_defaults: when True, rows that are missing values which
         generate defaults, namely integer primary key defaults and sequences,
         will be inserted **one at a time**, so that the primary key value
         is available.  In particular this will allow joined-inheritance
         and other multi-table mappings to insert correctly without the need
         to provide primary
         key values ahead of time; however,
         :paramref:`.Session.bulk_insert_mappings.return_defaults`
         **greatly reduces the performance gains** of the method overall.
         If the rows
         to be inserted only refer to a single table, then there is no
         reason this flag should be set as the returned default information
         is not used.

        :param render_nulls: When True, a value of ``None`` will result
         in a NULL value being included in the INSERT statement, rather
         than the column being omitted from the INSERT.   This allows all
         the rows being INSERTed to have the identical set of columns which
         allows the full set of rows to be batched to the DBAPI.  Normally,
         each column-set that contains a different combination of NULL values
         than the previous row must omit a different series of columns from
         the rendered INSERT statement, which means it must be emitted as a
         separate statement.   By passing this flag, the full set of rows
         are guaranteed to be batchable into one batch; the cost however is
         that server-side defaults which are invoked by an omitted column will
         be skipped, so care must be taken to ensure that these are not
         necessary.

         .. warning::

            When this flag is set, **server side default SQL values will
            not be invoked** for those columns that are inserted as NULL;
            the NULL value will be sent explicitly.   Care must be taken
            to ensure that no server-side default functions need to be
            invoked for the operation as a whole.

         .. versionadded:: 1.1

        .. seealso::

            :ref:`bulk_operations`

            :meth:`.Session.bulk_save_objects`

            :meth:`.Session.bulk_update_mappings`

        """
        self._bulk_save_mappings(
            mapper,
            mappings,
            False,
            False,
            return_defaults,
            False,
            render_nulls,
        )

    def bulk_update_mappings(self, mapper, mappings):
        """Perform a bulk update of the given list of mapping dictionaries.

        The bulk update feature allows plain Python dictionaries to be used as
        the source of simple UPDATE operations which can be more easily
        grouped together into higher performing "executemany"
        operations.  Using dictionaries, there is no "history" or session
        state management features in use, reducing latency when updating
        large numbers of simple rows.

        .. versionadded:: 1.0.0

        .. warning::

            The bulk update feature allows for a lower-latency UPDATE
            of rows at the expense of most other unit-of-work features.
            Features such as object management, relationship handling,
            and SQL clause support are **silently omitted** in favor of raw
            UPDATES of records.

            **Please read the list of caveats at**
            :ref:`bulk_operations_caveats` **before using this method, and
            fully test and confirm the functionality of all code developed
            using these systems.**

        :param mapper: a mapped class, or the actual :class:`_orm.Mapper`
         object,
         representing the single kind of object represented within the mapping
         list.

        :param mappings: a sequence of dictionaries, each one containing the
         state of the mapped row to be updated, in terms of the attribute names
         on the mapped class.   If the mapping refers to multiple tables, such
         as a joined-inheritance mapping, each dictionary may contain keys
         corresponding to all tables.   All those keys which are present and
         are not part of the primary key are applied to the SET clause of the
         UPDATE statement; the primary key values, which are required, are
         applied to the WHERE clause.


        .. seealso::

            :ref:`bulk_operations`

            :meth:`.Session.bulk_insert_mappings`

            :meth:`.Session.bulk_save_objects`

        """
        self._bulk_save_mappings(
            mapper, mappings, True, False, False, False, False
        )

    def _bulk_save_mappings(
        self,
        mapper,
        mappings,
        isupdate,
        isstates,
        return_defaults,
        update_changed_only,
        render_nulls,
    ):
        mapper = _class_to_mapper(mapper)
        self._flushing = True

        transaction = self.begin(_subtrans=True)
        try:
            if isupdate:
                persistence._bulk_update(
                    mapper,
                    mappings,
                    transaction,
                    isstates,
                    update_changed_only,
                )
            else:
                persistence._bulk_insert(
                    mapper,
                    mappings,
                    transaction,
                    isstates,
                    return_defaults,
                    render_nulls,
                )
            transaction.commit()

        except:
            with util.safe_reraise():
                transaction.rollback(_capture_exception=True)
        finally:
            self._flushing = False

    def is_modified(self, instance, include_collections=True):
        r"""Return ``True`` if the given instance has locally
        modified attributes.

        This method retrieves the history for each instrumented
        attribute on the instance and performs a comparison of the current
        value to its previously committed value, if any.

        It is in effect a more expensive and accurate
        version of checking for the given instance in the
        :attr:`.Session.dirty` collection; a full test for
        each attribute's net "dirty" status is performed.

        E.g.::

            return session.is_modified(someobject)

        A few caveats to this method apply:

        * Instances present in the :attr:`.Session.dirty` collection may
          report ``False`` when tested with this method.  This is because
          the object may have received change events via attribute mutation,
          thus placing it in :attr:`.Session.dirty`, but ultimately the state
          is the same as that loaded from the database, resulting in no net
          change here.
        * Scalar attributes may not have recorded the previously set
          value when a new value was applied, if the attribute was not loaded,
          or was expired, at the time the new value was received - in these
          cases, the attribute is assumed to have a change, even if there is
          ultimately no net change against its database value. SQLAlchemy in
          most cases does not need the "old" value when a set event occurs, so
          it skips the expense of a SQL call if the old value isn't present,
          based on the assumption that an UPDATE of the scalar value is
          usually needed, and in those few cases where it isn't, is less
          expensive on average than issuing a defensive SELECT.

          The "old" value is fetched unconditionally upon set only if the
          attribute container has the ``active_history`` flag set to ``True``.
          This flag is set typically for primary key attributes and scalar
          object references that are not a simple many-to-one.  To set this
          flag for any arbitrary mapped column, use the ``active_history``
          argument with :func:`.column_property`.

        :param instance: mapped instance to be tested for pending changes.
        :param include_collections: Indicates if multivalued collections
         should be included in the operation.  Setting this to ``False`` is a
         way to detect only local-column based properties (i.e. scalar columns
         or many-to-one foreign keys) that would result in an UPDATE for this
         instance upon flush.

        """
        state = object_state(instance)

        if not state.modified:
            return False

        dict_ = state.dict

        for attr in state.manager.attributes:
            if (
                not include_collections
                and hasattr(attr.impl, "get_collection")
            ) or not hasattr(attr.impl, "get_history"):
                continue

            (added, unchanged, deleted) = attr.impl.get_history(
                state, dict_, passive=attributes.NO_CHANGE
            )

            if added or deleted:
                return True
        else:
            return False

    @property
    def is_active(self):
        """True if this :class:`.Session` not in "partial rollback" state.

        .. versionchanged:: 1.4 The :class:`_orm.Session` no longer begins
           a new transaction immediately, so this attribute will be False
           when the :class:`_orm.Session` is first instantiated.

        "partial rollback" state typically indicates that the flush process
        of the :class:`_orm.Session` has failed, and that the
        :meth:`_orm.Session.rollback` method must be emitted in order to
        fully roll back the transaction.

        If this :class:`_orm.Session` is not in a transaction at all, the
        :class:`_orm.Session` will autobegin when it is first used, so in this
        case :attr:`_orm.Session.is_active` will return True.

        Otherwise, if this :class:`_orm.Session` is within a transaction,
        and that transaction has not been rolled back internally, the
        :attr:`_orm.Session.is_active` will also return True.

        .. seealso::

            :ref:`faq_session_rollback`

            :meth:`_orm.Session.in_transaction`

        """
        if self.autocommit:
            return (
                self._transaction is not None and self._transaction.is_active
            )
        else:
            return self._transaction is None or self._transaction.is_active

    identity_map = None
    """A mapping of object identities to objects themselves.

    Iterating through ``Session.identity_map.values()`` provides
    access to the full set of persistent objects (i.e., those
    that have row identity) currently in the session.

    .. seealso::

        :func:`.identity_key` - helper function to produce the keys used
        in this dictionary.

    """

    @property
    def _dirty_states(self):
        """The set of all persistent states considered dirty.

        This method returns all states that were modified including
        those that were possibly deleted.

        """
        return self.identity_map._dirty_states()

    @property
    def dirty(self):
        """The set of all persistent instances considered dirty.

        E.g.::

            some_mapped_object in session.dirty

        Instances are considered dirty when they were modified but not
        deleted.

        Note that this 'dirty' calculation is 'optimistic'; most
        attribute-setting or collection modification operations will
        mark an instance as 'dirty' and place it in this set, even if
        there is no net change to the attribute's value.  At flush
        time, the value of each attribute is compared to its
        previously saved value, and if there's no net change, no SQL
        operation will occur (this is a more expensive operation so
        it's only done at flush time).

        To check if an instance has actionable net changes to its
        attributes, use the :meth:`.Session.is_modified` method.

        """
        return util.IdentitySet(
            [
                state.obj()
                for state in self._dirty_states
                if state not in self._deleted
            ]
        )

    @property
    def deleted(self):
        "The set of all instances marked as 'deleted' within this ``Session``"

        return util.IdentitySet(list(self._deleted.values()))

    @property
    def new(self):
        "The set of all instances marked as 'new' within this ``Session``."

        return util.IdentitySet(list(self._new.values()))


class sessionmaker(_SessionClassMethods):
    """A configurable :class:`.Session` factory.

    The :class:`.sessionmaker` factory generates new
    :class:`.Session` objects when called, creating them given
    the configurational arguments established here.

    e.g.::

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        # an Engine, which the Session will use for connection
        # resources
        engine = create_engine('postgresql://scott:tiger@localhost/')

        Session = sessionmaker(engine)

        with Session() as session:
            session.add(some_object)
            session.add(some_other_object)
            session.commit()

    Context manager use is optional; otherwise, the returned
    :class:`_orm.Session` object may be closed explicitly via the
    :meth:`_orm.Session.close` method.   Using a
    ``try:/finally:`` block is optional, however will ensure that the close
    takes place even if there are database errors::

        session = Session()
        try:
            session.add(some_object)
            session.add(some_other_object)
            session.commit()
        finally:
            session.close()

    :class:`.sessionmaker` acts as a factory for :class:`_orm.Session`
    objects in the same way as an :class:`_engine.Engine` acts as a factory
    for :class:`_engine.Connection` objects.  In this way it also includes
    a :meth:`_orm.sessionmaker.begin` method, that provides a context
    manager which both begins and commits a transaction, as well as closes
    out the :class:`_orm.Session` when complete, rolling back the transaction
    if any errors occur::

        Session = sessionmaker(engine)

        with Session.begin() as session:
            session.add(some_object)
            session.add(some_other_object)
        # commits transaction, closes session

    .. versionadded:: 1.4

    When calling upon :class:`_orm.sessionmaker` to construct a
    :class:`_orm.Session`, keyword arguments may also be passed to the
    method; these arguments will override that of the globally configured
    parameters.  Below we use a :class:`_orm.sessionmaker` bound to a certain
    :class:`_engine.Engine` to produce a :class:`_orm.Session` that is instead
    bound to a specific :class:`_engine.Connection` procured from that engine::

        Session = sessionmaker(engine)

        # bind an individual session to a connection

        with engine.connect() as connection:
            with Session(bind=connection) as session:
                # work with session

    The class also includes a method :meth:`_orm.sessionmaker.configure`, which
    can be used to specify additional keyword arguments to the factory, which
    will take effect for subsequent :class:`.Session` objects generated. This
    is usually used to associate one or more :class:`_engine.Engine` objects
    with an existing
    :class:`.sessionmaker` factory before it is first used::

        # application starts, sessionmaker does not have
        # an engine bound yet
        Session = sessionmaker()

        # ... later, when an engine URL is read from a configuration
        # file or other events allow the engine to be created
        engine = create_engine('sqlite:///foo.db')
        Session.configure(bind=engine)

        sess = Session()
        # work with session

    .. seealso::

        :ref:`session_getting` - introductory text on creating
        sessions using :class:`.sessionmaker`.

    """

    def __init__(
        self,
        bind=None,
        class_=Session,
        autoflush=True,
        autocommit=False,
        expire_on_commit=True,
        info=None,
        **kw
    ):
        r"""Construct a new :class:`.sessionmaker`.

        All arguments here except for ``class_`` correspond to arguments
        accepted by :class:`.Session` directly.  See the
        :meth:`.Session.__init__` docstring for more details on parameters.

        :param bind: a :class:`_engine.Engine` or other :class:`.Connectable`
         with
         which newly created :class:`.Session` objects will be associated.
        :param class\_: class to use in order to create new :class:`.Session`
         objects.  Defaults to :class:`.Session`.
        :param autoflush: The autoflush setting to use with newly created
         :class:`.Session` objects.
        :param autocommit: The autocommit setting to use with newly created
         :class:`.Session` objects.
        :param expire_on_commit=True: the
         :paramref:`_orm.Session.expire_on_commit` setting to use
         with newly created :class:`.Session` objects.

        :param info: optional dictionary of information that will be available
         via :attr:`.Session.info`.  Note this dictionary is *updated*, not
         replaced, when the ``info`` parameter is specified to the specific
         :class:`.Session` construction operation.

        :param \**kw: all other keyword arguments are passed to the
         constructor of newly created :class:`.Session` objects.

        """
        kw["bind"] = bind
        kw["autoflush"] = autoflush
        kw["autocommit"] = autocommit
        kw["expire_on_commit"] = expire_on_commit
        if info is not None:
            kw["info"] = info
        self.kw = kw
        # make our own subclass of the given class, so that
        # events can be associated with it specifically.
        self.class_ = type(class_.__name__, (class_,), {})

    def begin(self):
        """Produce a context manager that both provides a new
        :class:`_orm.Session` as well as a transaction that commits.


        e.g.::

            Session = sessionmaker(some_engine)

            with Session.begin() as session:
                session.add(some_object)

            # commits transaction, closes session

        .. versionadded:: 1.4


        """

        session = self()
        return session._maker_context_manager()

    def __call__(self, **local_kw):
        """Produce a new :class:`.Session` object using the configuration
        established in this :class:`.sessionmaker`.

        In Python, the ``__call__`` method is invoked on an object when
        it is "called" in the same way as a function::

            Session = sessionmaker()
            session = Session()  # invokes sessionmaker.__call__()

        """
        for k, v in self.kw.items():
            if k == "info" and "info" in local_kw:
                d = v.copy()
                d.update(local_kw["info"])
                local_kw["info"] = d
            else:
                local_kw.setdefault(k, v)
        return self.class_(**local_kw)

    def configure(self, **new_kw):
        """(Re)configure the arguments for this sessionmaker.

        e.g.::

            Session = sessionmaker()

            Session.configure(bind=create_engine('sqlite://'))
        """
        self.kw.update(new_kw)

    def __repr__(self):
        return "%s(class_=%r, %s)" % (
            self.__class__.__name__,
            self.class_.__name__,
            ", ".join("%s=%r" % (k, v) for k, v in self.kw.items()),
        )


def close_all_sessions():
    """Close all sessions in memory.

    This function consults a global registry of all :class:`.Session` objects
    and calls :meth:`.Session.close` on them, which resets them to a clean
    state.

    This function is not for general use but may be useful for test suites
    within the teardown scheme.

    .. versionadded:: 1.3

    """

    for sess in _sessions.values():
        sess.close()


def make_transient(instance):
    """Alter the state of the given instance so that it is :term:`transient`.

    .. note::

        :func:`.make_transient` is a special-case function for
        advanced use cases only.

    The given mapped instance is assumed to be in the :term:`persistent` or
    :term:`detached` state.   The function will remove its association with any
    :class:`.Session` as well as its :attr:`.InstanceState.identity`. The
    effect is that the object will behave as though it were newly constructed,
    except retaining any attribute / collection values that were loaded at the
    time of the call.   The :attr:`.InstanceState.deleted` flag is also reset
    if this object had been deleted as a result of using
    :meth:`.Session.delete`.

    .. warning::

        :func:`.make_transient` does **not** "unexpire" or otherwise eagerly
        load ORM-mapped attributes that are not currently loaded at the time
        the function is called.   This includes attributes which:

        * were expired via :meth:`.Session.expire`

        * were expired as the natural effect of committing a session
          transaction, e.g. :meth:`.Session.commit`

        * are normally :term:`lazy loaded` but are not currently loaded

        * are "deferred" via :ref:`deferred` and are not yet loaded

        * were not present in the query which loaded this object, such as that
          which is common in joined table inheritance and other scenarios.

        After :func:`.make_transient` is called, unloaded attributes such
        as those above will normally resolve to the value ``None`` when
        accessed, or an empty collection for a collection-oriented attribute.
        As the object is transient and un-associated with any database
        identity, it will no longer retrieve these values.

    .. seealso::

        :func:`.make_transient_to_detached`

    """
    state = attributes.instance_state(instance)
    s = _state_session(state)
    if s:
        s._expunge_states([state])

    # remove expired state
    state.expired_attributes.clear()

    # remove deferred callables
    if state.callables:
        del state.callables

    if state.key:
        del state.key
    if state._deleted:
        del state._deleted


def make_transient_to_detached(instance):
    """Make the given transient instance :term:`detached`.

    .. note::

        :func:`.make_transient_to_detached` is a special-case function for
        advanced use cases only.

    All attribute history on the given instance
    will be reset as though the instance were freshly loaded
    from a query.  Missing attributes will be marked as expired.
    The primary key attributes of the object, which are required, will be made
    into the "key" of the instance.

    The object can then be added to a session, or merged
    possibly with the load=False flag, at which point it will look
    as if it were loaded that way, without emitting SQL.

    This is a special use case function that differs from a normal
    call to :meth:`.Session.merge` in that a given persistent state
    can be manufactured without any SQL calls.

    .. seealso::

        :func:`.make_transient`

        :meth:`.Session.enable_relationship_loading`

    """
    state = attributes.instance_state(instance)
    if state.session_id or state.key:
        raise sa_exc.InvalidRequestError("Given object must be transient")
    state.key = state.mapper._identity_key_from_state(state)
    if state._deleted:
        del state._deleted
    state._commit_all(state.dict)
    state._expire_attributes(state.dict, state.unloaded_expirable)


def object_session(instance):
    """Return the :class:`.Session` to which the given instance belongs.

    This is essentially the same as the :attr:`.InstanceState.session`
    accessor.  See that attribute for details.

    """

    try:
        state = attributes.instance_state(instance)
    except exc.NO_STATE as err:
        util.raise_(
            exc.UnmappedInstanceError(instance),
            replace_context=err,
        )
    else:
        return _state_session(state)


_new_sessionid = util.counter()
