"""Session CRUD operations."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import and_, func, or_
from sqlmodel import select, col

from openclose.storage.db import Database
from openclose.storage.schema import Session, Message, MessagePart
from openclose.id import new_session_id, new_message_id, new_part_id
from openclose.session.message import MessageRole, MessagePartType
from openclose.log import get_logger

log = get_logger(__name__)


class SessionManager:
    """Manages session lifecycle and message persistence."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def create_session(
        self,
        title: str = "",
        agent: str = "build",
        model: str = "",
        project_id: str = "",
    ) -> Session:
        """Create a new session."""
        session = Session(
            id=new_session_id(),
            title=title,
            agent=agent,
            model=model,
            project_id=project_id,
        )
        with self._db.get_session() as db:
            db.add(session)
            db.commit()
            db.refresh(session)
        log.info("Created session %s", session.id)
        return session

    def fork_session(
        self,
        source_session_id: str,
        agent: str | None = None,
        up_to_message_id: str = "",
        up_to_part_id: str = "",
    ) -> Session | None:
        """Fork a session: create a new session copying messages and
        message parts from the source. When ``agent`` is None, inherit the
        source's agent.

        ``up_to_message_id`` truncates the message list at that message
        (inclusive). ``up_to_part_id`` further truncates *within* the target
        message — only parts whose id <= ``up_to_part_id`` are copied. Since
        a single DB message can render as multiple UI bubbles (segments
        split on tool_result boundaries), part-level truncation is required
        for fork-from-bubble to match the user's visual selection. When
        parts are truncated, the assistant message's ``content`` is
        recomputed from the kept TEXT parts so LLM-reconstruction stays in
        sync with the parts.
        """
        with self._db.get_session() as db:
            source = db.get(Session, source_session_id)
            if source is None:
                return None

            if up_to_message_id:
                target = db.get(Message, up_to_message_id)
                if target is None or target.session_id != source_session_id:
                    return None

            if up_to_part_id:
                if not up_to_message_id:
                    return None
                target_part = db.get(MessagePart, up_to_part_id)
                if target_part is None or target_part.message_id != up_to_message_id:
                    return None

            new_agent = agent if agent is not None else source.agent
            new_session = Session(
                id=new_session_id(),
                title=source.title,
                agent=new_agent,
                model=source.model,
                project_id=source.project_id,
            )
            db.add(new_session)
            db.flush()

            # Copy messages (optionally truncated at up_to_message_id)
            stmt = (
                select(Message)
                .where(Message.session_id == source_session_id)
                .order_by(col(Message.created_at), col(Message.id))
            )
            if up_to_message_id:
                stmt = stmt.where(col(Message.id) <= up_to_message_id)
            old_messages = list(db.exec(stmt).all())
            msg_id_map: dict[str, str] = {}
            for old_msg in old_messages:
                new_mid = new_message_id()
                msg_id_map[old_msg.id] = new_mid
                db.add(Message(
                    id=new_mid,
                    session_id=new_session.id,
                    role=old_msg.role,
                    content=old_msg.content,
                    model=old_msg.model,
                    token_count=old_msg.token_count,
                ))

            # Copy message parts (optionally truncated within the target
            # message at up_to_part_id).
            if msg_id_map:
                old_parts = list(
                    db.exec(
                        select(MessagePart)
                        .where(col(MessagePart.message_id).in_(list(msg_id_map.keys())))
                        .order_by(col(MessagePart.created_at), col(MessagePart.id))
                    ).all()
                )
                if up_to_part_id:
                    old_parts = [
                        p for p in old_parts
                        if p.message_id != up_to_message_id or p.id <= up_to_part_id
                    ]
                # Track the new target message id so we can rewrite its
                # content from the kept TEXT parts.
                new_target_mid = msg_id_map.get(up_to_message_id) if up_to_part_id else None
                kept_text_for_target: list[str] = []
                for old_part in old_parts:
                    mapped_mid = msg_id_map[old_part.message_id]
                    db.add(MessagePart(
                        id=new_part_id(),
                        message_id=mapped_mid,
                        part_type=old_part.part_type,
                        content=old_part.content,
                        tool_name=old_part.tool_name,
                        tool_call_id=old_part.tool_call_id,
                        metadata_json=old_part.metadata_json,
                    ))
                    if new_target_mid and mapped_mid == new_target_mid \
                            and old_part.part_type == "text":
                        kept_text_for_target.append(old_part.content or "")
                if new_target_mid is not None:
                    new_msg = db.get(Message, new_target_mid)
                    if new_msg is not None:
                        new_msg.content = "".join(kept_text_for_target)

            db.commit()
            db.refresh(new_session)
        log.info(
            "Forked session %s → %s (agent=%s, up_to_message=%s, up_to_part=%s)",
            source_session_id, new_session.id, new_agent,
            up_to_message_id or "-", up_to_part_id or "-",
        )
        return new_session

    def get_session(self, session_id: str) -> Session | None:
        """Get a session by ID."""
        with self._db.get_session() as db:
            return db.get(Session, session_id)

    def list_sessions(self, include_archived: bool = False) -> list[Session]:
        """List all sessions, optionally including archived ones."""
        with self._db.get_session() as db:
            stmt = select(Session).order_by(col(Session.updated_at).desc())
            if not include_archived:
                stmt = stmt.where(Session.archived == False)  # noqa: E712
            return list(db.exec(stmt).all())

    def search_sessions(self, query: str, include_archived: bool = False) -> list[Session]:
        """Search sessions by title or by message content (text + reasoning parts).

        Empty/whitespace query falls back to ``list_sessions``.
        """
        if not query.strip():
            return self.list_sessions(include_archived=include_archived)
        escaped = (
            query.lower()
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        pattern = f"%{escaped}%"
        searchable_parts = [MessagePartType.TEXT.value, MessagePartType.REASONING.value]
        with self._db.get_session() as db:
            stmt = (
                select(Session)
                .outerjoin(Message, col(Message.session_id) == col(Session.id))
                .outerjoin(MessagePart, col(MessagePart.message_id) == col(Message.id))
                .where(
                    or_(
                        func.lower(Session.title).like(pattern, escape="\\"),
                        func.lower(Message.content).like(pattern, escape="\\"),
                        and_(
                            func.lower(MessagePart.content).like(pattern, escape="\\"),
                            col(MessagePart.part_type).in_(searchable_parts),
                        ),
                    )
                )
                .distinct()
                .order_by(col(Session.updated_at).desc())
            )
            if not include_archived:
                stmt = stmt.where(Session.archived == False)  # noqa: E712
            return list(db.exec(stmt).all())

    def archive_session(self, session_id: str) -> bool:
        """Archive a session."""
        with self._db.get_session() as db:
            session = db.get(Session, session_id)
            if session is None:
                return False
            session.archived = True
            session.updated_at = datetime.now(timezone.utc)
            db.add(session)
            db.commit()
        return True

    def update_title(self, session_id: str, title: str) -> bool:
        """Update session title."""
        with self._db.get_session() as db:
            session = db.get(Session, session_id)
            if session is None:
                return False
            session.title = title
            session.updated_at = datetime.now(timezone.utc)
            db.add(session)
            db.commit()
        return True

    def update_agent(self, session_id: str, agent: str) -> bool:
        """Switch a session's agent in-place."""
        with self._db.get_session() as db:
            session = db.get(Session, session_id)
            if session is None:
                return False
            session.agent = agent
            session.updated_at = datetime.now(timezone.utc)
            db.add(session)
            db.commit()
        return True

    def update_provider_and_model(
        self, session_id: str, provider: str, model: str,
    ) -> bool:
        """Switch a session's provider and model in-place (single tx)."""
        with self._db.get_session() as db:
            session = db.get(Session, session_id)
            if session is None:
                return False
            session.provider = provider
            session.model = model
            session.updated_at = datetime.now(timezone.utc)
            db.add(session)
            db.commit()
        return True

    def update_plan_in_context(self, session_id: str, enabled: bool) -> bool:
        """Toggle plan_in_context for a session."""
        with self._db.get_session() as db:
            session = db.get(Session, session_id)
            if session is None:
                return False
            session.plan_in_context = enabled
            session.updated_at = datetime.now(timezone.utc)
            db.add(session)
            db.commit()
        return True

    def update_video_compatible(self, session_id: str, enabled: bool) -> bool:
        """Toggle video_compatible for a session."""
        with self._db.get_session() as db:
            session = db.get(Session, session_id)
            if session is None:
                return False
            session.video_compatible = enabled
            session.updated_at = datetime.now(timezone.utc)
            db.add(session)
            db.commit()
        return True

    def add_message(
        self,
        session_id: str,
        role: MessageRole,
        content: str = "",
        model: str = "",
        token_count: int = 0,
    ) -> Message:
        """Add a message to a session."""
        msg = Message(
            id=new_message_id(),
            session_id=session_id,
            role=role.value,
            content=content,
            model=model,
            token_count=token_count,
        )
        with self._db.get_session() as db:
            db.add(msg)
            # Update session timestamp
            session = db.get(Session, session_id)
            if session:
                session.updated_at = datetime.now(timezone.utc)
                db.add(session)
            db.commit()
            db.refresh(msg)
        return msg

    def update_message_content(self, message_id: str, content: str) -> None:
        """Update the content of an existing message."""
        with self._db.get_session() as db:
            msg = db.get(Message, message_id)
            if msg:
                msg.content = content
                db.add(msg)
                db.commit()

    def delete_message(self, message_id: str) -> None:
        """Delete a message and its parts."""
        with self._db.get_session() as db:
            msg = db.get(Message, message_id)
            if msg:
                parts = db.exec(
                    select(MessagePart).where(MessagePart.message_id == message_id)
                ).all()
                for p in parts:
                    db.delete(p)
                db.delete(msg)
                db.commit()

    def add_message_part(
        self,
        message_id: str,
        part_type: MessagePartType,
        content: str = "",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
        metadata_json: str = "{}",
    ) -> MessagePart:
        """Add a part to a message."""
        part = MessagePart(
            id=new_part_id(),
            message_id=message_id,
            part_type=part_type.value,
            content=content,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            metadata_json=metadata_json,
        )
        with self._db.get_session() as db:
            db.add(part)
            db.commit()
            db.refresh(part)
        return part

    def get_messages(self, session_id: str) -> list[Message]:
        """Get all messages for a session, ordered by creation time."""
        with self._db.get_session() as db:
            return list(
                db.exec(
                    select(Message)
                    .where(Message.session_id == session_id)
                    .order_by(col(Message.created_at), col(Message.id))
                ).all()
            )

    def get_messages_with_parts(
        self, session_id: str
    ) -> list[tuple[Message, list[MessagePart]]]:
        """Get all messages for a session with their parts (two queries, no N+1)."""
        with self._db.get_session() as db:
            messages = list(
                db.exec(
                    select(Message)
                    .where(Message.session_id == session_id)
                    .order_by(col(Message.created_at), col(Message.id))
                ).all()
            )
            if not messages:
                return []
            msg_ids = [m.id for m in messages]
            parts = list(
                db.exec(
                    select(MessagePart)
                    .where(col(MessagePart.message_id).in_(msg_ids))
                    .order_by(col(MessagePart.created_at), col(MessagePart.id))
                ).all()
            )
        parts_by_msg: defaultdict[str, list[MessagePart]] = defaultdict(list)
        for p in parts:
            parts_by_msg[p.message_id].append(p)
        return [(m, parts_by_msg.get(m.id, [])) for m in messages]

    def get_message_parts(self, message_id: str) -> list[MessagePart]:
        """Get all parts for a message."""
        with self._db.get_session() as db:
            return list(
                db.exec(
                    select(MessagePart)
                    .where(MessagePart.message_id == message_id)
                    .order_by(col(MessagePart.created_at), col(MessagePart.id))
                ).all()
            )

    def get_empty_session(self, agent: str = "build") -> Session | None:
        """Find the most recent non-archived session with zero messages."""
        with self._db.get_session() as db:
            stmt = (
                select(Session)
                .where(
                    Session.agent == agent,
                    Session.archived == False,  # noqa: E712
                    ~select(Message.id)
                    .where(Message.session_id == Session.id)
                    .correlate(Session)
                    .exists(),
                )
                .order_by(col(Session.updated_at).desc())
                .limit(1)
            )
            return db.exec(stmt).first()

    def cleanup_empty_sessions(
        self, keep_session_id: str | None = None
    ) -> int:
        """Delete all empty sessions (0 messages), optionally keeping one.

        Returns the number of deleted sessions.
        """
        with self._db.get_session() as db:
            stmt = select(Session).where(
                Session.archived == False,  # noqa: E712
                ~select(Message.id)
                .where(Message.session_id == Session.id)
                .correlate(Session)
                .exists(),
            )
            if keep_session_id is not None:
                stmt = stmt.where(Session.id != keep_session_id)
            empties = list(db.exec(stmt).all())
            for s in empties:
                db.delete(s)
            if empties:
                db.commit()
            count = len(empties)
        if count:
            log.info("Cleaned up %d empty session(s)", count)
        return count

    def delete_session(self, session_id: str) -> bool:
        """Delete a session and all its messages."""
        with self._db.get_session() as db:
            # Delete parts
            messages = db.exec(
                select(Message).where(Message.session_id == session_id)
            ).all()
            for msg in messages:
                parts = db.exec(
                    select(MessagePart).where(MessagePart.message_id == msg.id)
                ).all()
                for part in parts:
                    db.delete(part)
                db.delete(msg)

            session = db.get(Session, session_id)
            if session is None:
                return False
            db.delete(session)
            db.commit()
        return True
