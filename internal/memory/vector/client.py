"""
Vector Memory Client - PostgreSQL + pgvector semantic search
"""
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
class VectorEntry:
    id: str
    text: str
    embedding: List[float]
    source_type: str
    metadata: Dict[str, str]
    created_at_ms: int


class VectorClient:
    """PostgreSQL + pgvector client for semantic memory"""
    
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
            logger.info(f"Connected to PostgreSQL at {self.db_host}:{self.db_port}")
        except Exception as e:
            logger.error(f"Failed to connect to database: {e}")
            raise

    async def disconnect(self):
        """Close database connection"""
        if self.conn:
            await self.conn.close()
            logger.info("Disconnected from PostgreSQL")

    async def write_vector(
        self,
        text: str,
        embedding: List[float],
        source_type: str,
        metadata: Optional[Dict[str, str]] = None,
    ) -> str:
        """Store semantic vector entry"""
        if not self.conn:
            raise RuntimeError("Not connected to database")
        
        vector_id = str(uuid.uuid4())
        now_ms = int(datetime.utcnow().timestamp() * 1000)
        metadata = metadata or {}
        
        try:
            await self.conn.execute(
                """
                INSERT INTO vector_entries (id, text, embedding, source_type, metadata, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (vector_id, text, embedding, source_type, metadata, now_ms, now_ms),
            )
            logger.debug(f"Wrote vector {vector_id} (source: {source_type})")
            return vector_id
        except Exception as e:
            logger.error(f"Failed to write vector: {e}")
            raise

    async def semantic_search(
        self,
        query_embedding: List[float],
        limit: int = 10,
        similarity_threshold: float = 0.7,
        source_type_filter: Optional[str] = None,
    ) -> List[VectorEntry]:
        """Search for similar vectors"""
        if not self.conn:
            raise RuntimeError("Not connected to database")
        
        try:
            query = """
                SELECT id, text, embedding, source_type, metadata, created_at
                FROM vector_entries
                WHERE 1=1
            """
            params = []
            
            if source_type_filter:
                query += " AND source_type = %s"
                params.append(source_type_filter)
            
            # Cosine similarity: 1 - (distance / 2) for normalized vectors
            query += """
                ORDER BY embedding <=> %s::vector ASC
                LIMIT %s
            """
            params.extend([query_embedding, limit])
            
            cur = await self.conn.execute(query, params)
            rows = await cur.fetchall()
            
            results = []
            for row in rows:
                results.append(VectorEntry(
                    id=row["id"],
                    text=row["text"],
                    embedding=row["embedding"],
                    source_type=row["source_type"],
                    metadata=row["metadata"],
                    created_at_ms=row["created_at"],
                ))
            
            logger.debug(f"Semantic search returned {len(results)} results")
            return results
        except Exception as e:
            logger.error(f"Semantic search failed: {e}")
            raise

    async def get_vector(self, vector_id: str) -> Optional[VectorEntry]:
        """Retrieve specific vector"""
        if not self.conn:
            raise RuntimeError("Not connected to database")
        
        try:
            cur = await self.conn.execute(
                "SELECT id, text, embedding, source_type, metadata, created_at FROM vector_entries WHERE id = %s",
                (vector_id,),
            )
            row = await cur.fetchone()
            
            if not row:
                return None
            
            return VectorEntry(
                id=row["id"],
                text=row["text"],
                embedding=row["embedding"],
                source_type=row["source_type"],
                metadata=row["metadata"],
                created_at_ms=row["created_at"],
            )
        except Exception as e:
            logger.error(f"Failed to retrieve vector: {e}")
            raise

    async def list_vectors(
        self,
        source_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[VectorEntry]:
        """List vectors with optional filtering"""
        if not self.conn:
            raise RuntimeError("Not connected to database")
        
        try:
            query = "SELECT id, text, embedding, source_type, metadata, created_at FROM vector_entries"
            params = []
            
            if source_type:
                query += " WHERE source_type = %s"
                params.append(source_type)
            
            query += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
            params.extend([limit, offset])
            
            cur = await self.conn.execute(query, params)
            rows = await cur.fetchall()
            
            results = []
            for row in rows:
                results.append(VectorEntry(
                    id=row["id"],
                    text=row["text"],
                    embedding=row["embedding"],
                    source_type=row["source_type"],
                    metadata=row["metadata"],
                    created_at_ms=row["created_at"],
                ))
            
            return results
        except Exception as e:
            logger.error(f"Failed to list vectors: {e}")
            raise

    async def delete_vector(self, vector_id: str) -> bool:
        """Delete vector entry"""
        if not self.conn:
            raise RuntimeError("Not connected to database")
        
        try:
            cur = await self.conn.execute(
                "DELETE FROM vector_entries WHERE id = %s",
                (vector_id,),
            )
            deleted = cur.rowcount > 0
            logger.debug(f"Deleted vector {vector_id}: {deleted}")
            return deleted
        except Exception as e:
            logger.error(f"Failed to delete vector: {e}")
            raise

    async def cleanup_expired_vectors(self, ttl_ms: int) -> int:
        """Clean up expired vectors (TTL-based)"""
        if not self.conn:
            raise RuntimeError("Not connected to database")
        
        try:
            cutoff_ms = int(datetime.utcnow().timestamp() * 1000) - ttl_ms
            cur = await self.conn.execute(
                "DELETE FROM vector_entries WHERE ttl_ms IS NOT NULL AND created_at < %s",
                (cutoff_ms,),
            )
            deleted_count = cur.rowcount
            logger.info(f"Cleaned up {deleted_count} expired vectors")
            return deleted_count
        except Exception as e:
            logger.error(f"Cleanup failed: {e}")
            raise
