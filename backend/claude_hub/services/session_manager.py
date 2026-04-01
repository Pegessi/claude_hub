from typing import Dict, Set
from fastapi import WebSocket
import logging

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages WebSocket connections."""

    def __init__(self):
        # tab_id -> set of WebSocket connections
        self.active_connections: Dict[str, Set[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, tab_id: str):
        """Connect a WebSocket to a tab."""
        await websocket.accept()
        if tab_id not in self.active_connections:
            self.active_connections[tab_id] = set()
        self.active_connections[tab_id].add(websocket)
        logger.info(f"WebSocket connected to tab {tab_id}")

    def disconnect(self, websocket: WebSocket, tab_id: str):
        """Disconnect a WebSocket from a tab."""
        if tab_id in self.active_connections:
            self.active_connections[tab_id].discard(websocket)
            if not self.active_connections[tab_id]:
                del self.active_connections[tab_id]
            logger.info(f"WebSocket disconnected from tab {tab_id}")

    async def send_personal_message(self, message: str, websocket: WebSocket):
        """Send a message to a specific WebSocket."""
        await websocket.send_text(message)

    async def broadcast_to_tab(self, tab_id: str, message: str):
        """Broadcast a message to all connections in a tab."""
        if tab_id not in self.active_connections:
            return
        disconnected = set()
        for connection in self.active_connections[tab_id]:
            try:
                await connection.send_text(message)
            except Exception:
                disconnected.add(connection)
        for connection in disconnected:
            self.disconnect(connection, tab_id)


# Global connection manager
connection_manager = ConnectionManager()
