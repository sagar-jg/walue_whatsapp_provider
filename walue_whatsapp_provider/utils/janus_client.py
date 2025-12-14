"""
Janus WebRTC Gateway Client

HTTP API client for interacting with Janus WebRTC server.
Used for creating sessions, attaching plugins, and managing WebRTC calls.

Janus HTTP API Documentation:
- Create session: POST /janus
- Attach plugin: POST /janus/{session_id}
- Send message: POST /janus/{session_id}/{handle_id}
- Destroy session: POST /janus/{session_id}
"""

import frappe
import requests
import secrets
from typing import Optional
from dataclasses import dataclass


@dataclass
class JanusSession:
    """Represents a Janus session with plugin handle"""
    session_id: str
    handle_id: str
    room_id: Optional[int] = None


class JanusClient:
    """
    HTTP API Client for Janus WebRTC Gateway

    Handles session management, plugin attachment, and SDP negotiation
    for WhatsApp Business Calling integration.
    """

    def __init__(self, http_url: str = None, api_secret: str = None, admin_secret: str = None):
        """
        Initialize Janus client

        Args:
            http_url: Janus HTTP API URL (e.g., http://janus.example.com:8088/janus)
            api_secret: API secret for authenticated requests
            admin_secret: Admin secret for admin API calls
        """
        if not http_url:
            settings = frappe.get_single("WhatsApp Provider Settings")

            # Prefer explicit HTTP URL if configured
            if settings.janus_http_url:
                http_url = settings.janus_http_url
            else:
                # Convert WebSocket URL to HTTP if needed
                ws_url = settings.janus_ws_url or ""
                if ws_url.startswith("wss://"):
                    http_url = ws_url.replace("wss://", "https://").replace(":8989", ":8088")
                elif ws_url.startswith("ws://"):
                    http_url = ws_url.replace("ws://", "http://").replace(":8188", ":8088")
                else:
                    http_url = ws_url

            # Ensure /janus endpoint
            if http_url and not http_url.endswith("/janus"):
                http_url = http_url.rstrip("/") + "/janus"

            api_secret = settings.get_password("janus_api_secret") if settings.janus_api_secret else None
            admin_secret = settings.get_password("janus_admin_secret") if settings.janus_admin_secret else None

        self.http_url = http_url
        self.api_secret = api_secret
        self.admin_secret = admin_secret
        self.timeout = 10

    def _generate_transaction_id(self) -> str:
        """Generate unique transaction ID for Janus requests"""
        return secrets.token_hex(12)

    def _make_request(self, endpoint: str = "", payload: dict = None, method: str = "POST") -> dict:
        """
        Make HTTP request to Janus API

        Args:
            endpoint: URL path after base URL
            payload: JSON payload
            method: HTTP method

        Returns:
            dict: Janus response
        """
        url = f"{self.http_url}{endpoint}"

        if payload is None:
            payload = {}

        # Add transaction ID
        payload["transaction"] = self._generate_transaction_id()

        # Add API secret if configured
        if self.api_secret:
            payload["apisecret"] = self.api_secret

        try:
            if method == "POST":
                response = requests.post(url, json=payload, timeout=self.timeout)
            else:
                response = requests.get(url, params=payload, timeout=self.timeout)

            response.raise_for_status()
            data = response.json()

            if data.get("janus") == "error":
                error = data.get("error", {})
                raise JanusError(
                    error.get("code", 0),
                    error.get("reason", "Unknown Janus error")
                )

            return data

        except requests.RequestException as e:
            frappe.log_error(f"Janus API request failed: {str(e)}", "Janus Client")
            raise JanusError(0, f"Connection failed: {str(e)}")

    def create_session(self) -> str:
        """
        Create a new Janus session

        Returns:
            str: Session ID
        """
        response = self._make_request(payload={"janus": "create"})

        if response.get("janus") != "success":
            raise JanusError(0, "Failed to create session")

        session_id = response.get("data", {}).get("id")
        if not session_id:
            raise JanusError(0, "No session ID in response")

        return str(session_id)

    def attach_plugin(self, session_id: str, plugin: str = "janus.plugin.audiobridge") -> str:
        """
        Attach a plugin to a session

        Args:
            session_id: Janus session ID
            plugin: Plugin identifier (audiobridge, videoroom, sip, etc.)

        Returns:
            str: Plugin handle ID
        """
        response = self._make_request(
            endpoint=f"/{session_id}",
            payload={
                "janus": "attach",
                "plugin": plugin
            }
        )

        if response.get("janus") != "success":
            raise JanusError(0, f"Failed to attach plugin: {plugin}")

        handle_id = response.get("data", {}).get("id")
        if not handle_id:
            raise JanusError(0, "No handle ID in response")

        return str(handle_id)

    def create_audiobridge_room(self, session_id: str, handle_id: str, room_id: int = None) -> int:
        """
        Create an AudioBridge room for a call

        Args:
            session_id: Janus session ID
            handle_id: AudioBridge plugin handle ID
            room_id: Optional specific room ID (auto-generated if not provided)

        Returns:
            int: Room ID
        """
        if room_id is None:
            room_id = secrets.randbelow(900000000) + 100000000  # 9-digit random

        response = self._make_request(
            endpoint=f"/{session_id}/{handle_id}",
            payload={
                "janus": "message",
                "body": {
                    "request": "create",
                    "room": room_id,
                    "description": f"WhatsApp Call Room {room_id}",
                    "is_private": True,
                    "sampling_rate": 16000,  # WhatsApp uses 16kHz
                    "audiolevel_ext": True,
                    "audiolevel_event": True,
                    "record": False,  # Can be enabled for recording
                    "permanent": False  # Temporary room
                }
            }
        )

        # Check for success in plugindata
        plugindata = response.get("plugindata", {}).get("data", {})
        if plugindata.get("audiobridge") == "created":
            return plugindata.get("room", room_id)
        elif plugindata.get("error"):
            raise JanusError(plugindata.get("error_code", 0), plugindata.get("error"))

        return room_id

    def join_audiobridge_room(
        self,
        session_id: str,
        handle_id: str,
        room_id: int,
        display: str = "WhatsApp User",
        muted: bool = False
    ) -> dict:
        """
        Join an AudioBridge room

        Args:
            session_id: Janus session ID
            handle_id: AudioBridge plugin handle ID
            room_id: Room to join
            display: Display name
            muted: Start muted

        Returns:
            dict: Join response with participant info
        """
        response = self._make_request(
            endpoint=f"/{session_id}/{handle_id}",
            payload={
                "janus": "message",
                "body": {
                    "request": "join",
                    "room": room_id,
                    "display": display,
                    "muted": muted
                }
            }
        )

        plugindata = response.get("plugindata", {}).get("data", {})
        if plugindata.get("audiobridge") == "joined":
            return {
                "room": plugindata.get("room"),
                "id": plugindata.get("id"),
                "participants": plugindata.get("participants", [])
            }
        elif plugindata.get("error"):
            raise JanusError(plugindata.get("error_code", 0), plugindata.get("error"))

        return plugindata

    def configure_webrtc(
        self,
        session_id: str,
        handle_id: str,
        sdp_offer: str,
        muted: bool = False
    ) -> str:
        """
        Configure WebRTC connection with SDP offer

        Args:
            session_id: Janus session ID
            handle_id: Plugin handle ID
            sdp_offer: SDP offer from client
            muted: Start with audio muted

        Returns:
            str: SDP answer from Janus
        """
        response = self._make_request(
            endpoint=f"/{session_id}/{handle_id}",
            payload={
                "janus": "message",
                "body": {
                    "request": "configure",
                    "muted": muted
                },
                "jsep": {
                    "type": "offer",
                    "sdp": sdp_offer
                }
            }
        )

        # Get SDP answer from response
        jsep = response.get("jsep", {})
        if jsep.get("type") == "answer":
            return jsep.get("sdp")

        # Sometimes Janus returns event, need to poll
        plugindata = response.get("plugindata", {}).get("data", {})
        if plugindata.get("error"):
            raise JanusError(plugindata.get("error_code", 0), plugindata.get("error"))

        raise JanusError(0, "No SDP answer received from Janus")

    def trickle_candidate(
        self,
        session_id: str,
        handle_id: str,
        candidate: dict = None
    ):
        """
        Send ICE candidate (trickle)

        Args:
            session_id: Janus session ID
            handle_id: Plugin handle ID
            candidate: ICE candidate dict, or None for end-of-candidates
        """
        payload = {
            "janus": "trickle"
        }

        if candidate:
            payload["candidate"] = candidate
        else:
            payload["candidate"] = {"completed": True}

        self._make_request(
            endpoint=f"/{session_id}/{handle_id}",
            payload=payload
        )

    def destroy_room(self, session_id: str, handle_id: str, room_id: int):
        """
        Destroy an AudioBridge room

        Args:
            session_id: Janus session ID
            handle_id: AudioBridge handle ID
            room_id: Room to destroy
        """
        try:
            self._make_request(
                endpoint=f"/{session_id}/{handle_id}",
                payload={
                    "janus": "message",
                    "body": {
                        "request": "destroy",
                        "room": room_id
                    }
                }
            )
        except JanusError as e:
            # Room might already be destroyed
            frappe.log_error(f"Room destroy warning: {str(e)}", "Janus Client")

    def detach_handle(self, session_id: str, handle_id: str):
        """
        Detach a plugin handle

        Args:
            session_id: Janus session ID
            handle_id: Handle to detach
        """
        try:
            self._make_request(
                endpoint=f"/{session_id}/{handle_id}",
                payload={"janus": "detach"}
            )
        except JanusError as e:
            frappe.log_error(f"Handle detach warning: {str(e)}", "Janus Client")

    def destroy_session(self, session_id: str):
        """
        Destroy a Janus session

        Args:
            session_id: Session to destroy
        """
        try:
            self._make_request(
                endpoint=f"/{session_id}",
                payload={"janus": "destroy"}
            )
        except JanusError as e:
            frappe.log_error(f"Session destroy warning: {str(e)}", "Janus Client")

    def keepalive(self, session_id: str):
        """
        Send keepalive to prevent session timeout

        Args:
            session_id: Session to keep alive
        """
        self._make_request(
            endpoint=f"/{session_id}",
            payload={"janus": "keepalive"}
        )

    def poll_events(self, session_id: str, timeout: int = 30) -> list:
        """
        Long-poll for events from Janus

        Args:
            session_id: Session ID
            timeout: Poll timeout in seconds

        Returns:
            list: Events received
        """
        try:
            response = requests.get(
                f"{self.http_url}/{session_id}",
                params={"maxev": 10},
                timeout=timeout + 5
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException:
            return []


class JanusError(Exception):
    """Janus API error"""

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"Janus error {code}: {message}")


def create_call_session(customer_id: str) -> JanusSession:
    """
    Create a complete Janus session for a WhatsApp call

    Creates session, attaches AudioBridge plugin, creates room.

    Args:
        customer_id: Customer identifier for logging

    Returns:
        JanusSession: Session with all IDs
    """
    client = JanusClient()

    # Create session
    session_id = client.create_session()

    # Attach AudioBridge plugin
    handle_id = client.attach_plugin(session_id, "janus.plugin.audiobridge")

    # Create room
    room_id = client.create_audiobridge_room(session_id, handle_id)

    return JanusSession(
        session_id=session_id,
        handle_id=handle_id,
        room_id=room_id
    )


def cleanup_call_session(session_id: str, handle_id: str, room_id: int = None):
    """
    Clean up a Janus call session

    Args:
        session_id: Session to cleanup
        handle_id: Handle to detach
        room_id: Room to destroy (optional)
    """
    client = JanusClient()

    if room_id:
        client.destroy_room(session_id, handle_id, room_id)

    client.detach_handle(session_id, handle_id)
    client.destroy_session(session_id)
