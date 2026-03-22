"""
Ledger Store - Immutable action log for decision audit trail
"""
import json
import logging
import uuid
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from datetime import datetime

try:
    import psycopg
    from psycopg import AsyncConnection
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None

logger = logging.getLogger(__name__)


@dataclass
class LedgerEntry:
    id: str
    timestamp_ms: int
    action_type: str
    actor_id: str
    request_id: Optional[str]
    details: str
    signature: Optional[str]
    metadata: Dict[str, Any]


class LedgerStore:
    """PostgreSQL ledger store for immutable decision log"""
    
    VALID_ACTION_TYPES = ["approve", "reject", "execute", "rollback", "retrospective"]
    
    def __init__(
        self,
        db_host: str,
        db_port: int,
        db_name: str,
        db_user: str,
        db_password: str,
    ):
        self.db_host = db_host
        self.db_port = db_port
        self.db_name = db_name
        self.db_user = db_user
        self.db_password = db_password
        self.conn: Optional[AsyncConnection] = None

    async def connect(self):
        """Establish database connection"""
        if psycopg is None:
            raise RuntimeError("psycopg not installed. Run: pip install psycopg[binary]")
        
        try:
            self.conn = await psycopg.AsyncConnection.connect(
                f"postgresql://{self.db_user}:{self.db_password}@"
                f"{self.db_host}:{self.db_port}/{self.db_name}",
                row_factory=dict_row,
                autocommit=True,
            )
            logger.info(f"Connected to Ledger store at {self.db_host}:{self.db_port}")
        except Exception as e:
            logger.error(f"Failed to connect to database: {e}")
            raise

    async def disconnect(self):
        """Close database connection"""
        if self.conn:
            await self.conn.close()
            logger.info("Disconnected from Ledger store")

    async def write_entry(
        self,
        action_type: str,
        actor_id: str,
        details: str,
        request_id: Optional[str] = None,
        signature: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Append immutable entry to ledger"""
        if not self.conn:
            raise RuntimeError("Not connected to database")
        
        if action_type not in self.VALID_ACTION_TYPES:
            raise ValueError(f"Invalid action_type: {action_type}")
        
        entry_id = str(uuid.uuid4())
        now_ms = int(datetime.utcnow().timestamp() * 1000)
        metadata = metadata or {}
        
        try:
            await self.conn.execute(
                """
                INSERT INTO ledger_entries 
                (id, timestamp_ms, action_type, actor_id, request_id, details, signature, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                (entry_id, now_ms, action_type, actor_id, request_id, details, signature, json.dumps(metadata)),
            )
            logger.info(f"Ledger entry {entry_id}: {action_type} by {actor_id}")
            return entry_id
        except Exception as e:
            logger.error(f"Failed to write ledger entry: {e}")
            raise

    async def query_entries(
        self,
        action_type_filter: Optional[str] = None,
        actor_id_filter: Optional[str] = None,
        request_id_filter: Optional[str] = None,
        start_time_ms: Optional[int] = None,
        end_time_ms: Optional[int] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[LedgerEntry]:
        """Query immutable ledger with filters"""
        if not self.conn:
            raise RuntimeError("Not connected to database")
        
        try:
            query = "SELECT id, timestamp_ms, action_type, actor_id, request_id, details, signature, metadata FROM ledger_entries WHERE 1=1"
            params = []
            
            if action_type_filter:
                query += " AND action_type = %s"
                params.append(action_type_filter)
            
            if actor_id_filter:
                query += " AND actor_id = %s"
                params.append(actor_id_filter)
            
            if request_id_filter:
                query += " AND request_id = %s"
                params.append(request_id_filter)
            
            if start_time_ms is not None:
                query += " AND timestamp_ms >= %s"
                params.append(start_time_ms)
            
            if end_time_ms is not None:
                query += " AND timestamp_ms <= %s"
                params.append(end_time_ms)
            
            query += " ORDER BY timestamp_ms DESC LIMIT %s OFFSET %s"
            params.extend([limit, offset])
            
            cur = await self.conn.execute(query, params)
            rows = await cur.fetchall()
            
            results = []
            for row in rows:
                results.append(LedgerEntry(
                    id=row["id"],
                    timestamp_ms=row["timestamp_ms"],
                    action_type=row["action_type"],
                    actor_id=row["actor_id"],
                    request_id=row["request_id"],
                    details=row["details"],
                    signature=row["signature"],
                    metadata=row["metadata"],
                ))
            
            return results
        except Exception as e:
            logger.error(f"Ledger query failed: {e}")
            raise

    async def get_entry(self, entry_id: str) -> Optional[LedgerEntry]:
        """Retrieve specific ledger entry"""
        if not self.conn:
            raise RuntimeError("Not connected to database")
        
        try:
            cur = await self.conn.execute(
                "SELECT id, timestamp_ms, action_type, actor_id, request_id, details, signature, metadata FROM ledger_entries WHERE id = %s",
                (entry_id,),
            )
            row = await cur.fetchone()
            
            if not row:
                return None
            
            return LedgerEntry(
                id=row["id"],
                timestamp_ms=row["timestamp_ms"],
                action_type=row["action_type"],
                actor_id=row["actor_id"],
                request_id=row["request_id"],
                details=row["details"],
                signature=row["signature"],
                metadata=row["metadata"],
            )
        except Exception as e:
            logger.error(f"Failed to retrieve ledger entry: {e}")
            raise

    async def get_approval_count(self) -> int:
        """Get total approval count"""
        if not self.conn:
            raise RuntimeError("Not connected to database")
        
        try:
            cur = await self.conn.execute(
                "SELECT COUNT(*) as count FROM ledger_entries WHERE action_type = 'approve'",
            )
            row = await cur.fetchone()
            return row["count"] if row else 0
        except Exception as e:
            logger.error(f"Failed to get approval count: {e}")
            raise

    async def get_rejection_count_24h(self) -> int:
        """Get rejections in last 24 hours"""
        if not self.conn:
            raise RuntimeError("Not connected to database")
        
        try:
            now_ms = int(datetime.utcnow().timestamp() * 1000)
            cutoff_ms = now_ms - (24 * 60 * 60 * 1000)
            
            cur = await self.conn.execute(
                "SELECT COUNT(*) as count FROM ledger_entries WHERE action_type = 'reject' AND timestamp_ms >= %s",
                (cutoff_ms,),
            )
            row = await cur.fetchone()
            return row["count"] if row else 0
        except Exception as e:
            logger.error(f"Failed to get rejection count: {e}")
            raise

    async def get_ledger_size(self) -> int:
        """Get total ledger size"""
        if not self.conn:
            raise RuntimeError("Not connected to database")
        
        try:
            cur = await self.conn.execute(
                "SELECT COUNT(*) as count FROM ledger_entries",
            )
            row = await cur.fetchone()
            return row["count"] if row else 0
        except Exception as e:
            logger.error(f"Failed to get ledger size: {e}")
            raise

    async def check_request_rejected_tier4(
        self,
        request_id: str,
    ) -> bool:
        """Check if Tier 4 rejection exists in 24h cache"""
        if not self.conn:
            raise RuntimeError("Not connected to database")
        
        try:
            now_ms = int(datetime.utcnow().timestamp() * 1000)
            cutoff_ms = now_ms - (24 * 60 * 60 * 1000)
            
            cur = await self.conn.execute(
                """
                SELECT COUNT(*) as count FROM ledger_entries 
                WHERE request_id = %s AND action_type = 'reject' 
                AND timestamp_ms >= %s
                LIMIT 1
                """,
                (request_id, cutoff_ms),
            )
            row = await cur.fetchone()
            
            return (row["count"] if row else 0) > 0
        except Exception as e:
            logger.error(f"Failed to check tier 4 cache: {e}")
            raise
