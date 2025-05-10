import asyncio
import websockets
from urllib.parse import urlparse, parse_qs
import json
import random
from datetime import datetime

class OCPPServer:
    def __init__(self, port):
        self.port = port
        self.chargers = {}

    async def start(self):
        # Use websockets.serve with the correct handler
        self.server = await websockets.serve(
            self.handle_connection,
            "0.0.0.0", 
            self.port
        )
        print(f"OCPP Server running on port {self.port}")
        await self.server.wait_closed()

    async def handle_connection(self, websocket, path=None):
        """Handle incoming WebSocket connection
        
        The path parameter is included for backwards compatibility.
        In newer websockets versions, only the websocket object is passed.
        """
        charger_id = None
        try:
            # Extract path if it's not provided directly (newer websockets versions)
            if path is None:
                # Try to get path from various possible locations
                if hasattr(websocket, "path"):
                    path = websocket.path
                elif hasattr(websocket, "request") and hasattr(websocket.request, "path"):
                    path = websocket.request.path
                elif hasattr(websocket, "request_headers") and "path" in websocket.request_headers:
                    path = websocket.request_headers["path"]
                else:
                    # As a last resort, try to extract from raw headers
                    path = ""
                    print("Warning: Could not determine path from websocket object")
            
            # Parse the charger ID from the path
            # query_params = parse_qs(urlparse(path).query)
            # charger_id = query_params.get('chargerId', [None])[0]

            # Extract chargerId from the URL path (e.g., /{chargerId})
            path_parts = path.strip('/').split('/')
            if len(path_parts) == 1:
                charger_id = path_parts[0]
            else:
                charger_id = None

            if not charger_id:
                print("No chargerId provided, closing connection")
                await websocket.close()
                return

            print(f"Charger connected: {charger_id}")
            self.chargers[charger_id] = websocket

            try:
                async for message in websocket:
                    await self.handle_message(charger_id, message)
            except websockets.exceptions.ConnectionClosed:
                await self.handle_disconnect(charger_id)
            except Exception as e:
                print(f"Error in connection handler: {e}")
        finally:
            if charger_id and charger_id in self.chargers:
                del self.chargers[charger_id]

    async def handle_message(self, charger_id, data):
        """Process incoming OCPP messages"""
        try:
            message = json.loads(data)
            if len(message) < 3:
                raise ValueError("Invalid message format")
                
            message_type, message_id, action = message[:3]

            if message_type != 2:  # CALL message type
                await self.send_error(charger_id, message_id, 'ProtocolError', 'Invalid message type')
                return

            print(f"[SERVER] Received from {charger_id}: {json.dumps(message)}")

            # Handle different OCPP actions
            response = None
            if action == 'BootNotification':
                response = {
                    "status": "Accepted",
                    "interval": 300,
                    "currentTime": datetime.utcnow().isoformat() + "Z"
                }
            elif action == 'Heartbeat':
                response = {
                    "currentTime": datetime.utcnow().isoformat() + "Z"
                }
            elif action == 'Authorize':
                payload = message[3]
                response = {
                    "idTagInfo": {
                        "status": "Accepted",
                        "expiryDate": datetime.utcnow().isoformat() + "Z",
                        "parentIdTag": payload.get('idTag', None),
                    }
                }
            elif action == 'StartTransaction':
                response = {
                    "transactionId": random.randint(1, 1000),
                    "idTagInfo": {"status": "Accepted"}
                }
            elif action in ['MeterValues' , 'StopTransaction'] :
                response = {"idTagInfo": {"status": "Accepted"}}

            if response:
                await self.send_response(charger_id, message_id, response)
            else:
                print(f"Action not supported: {action}")
                await self.send_error(charger_id, message_id, 'NotSupported', 'Action not supported')

        except Exception as error:
            message_id = message[1] if len(message) > 1 else 'unknown'
            await self.send_error(charger_id, message_id, 'FormationViolation', str(error))

    async def send_response(self, charger_id, message_id, payload):
        """Send OCPP response"""
        response = [3, message_id, payload]  # CALLRESULT message type
        if charger_id in self.chargers:
            await self.chargers[charger_id].send(json.dumps(response))
            print(f"[SERVER] Sent to {charger_id}: {json.dumps(response)}")
    
    async def send_error(self, charger_id, message_id, error_code, error_description):
        """Send OCPP error"""
        error_message = [4, message_id, error_code, error_description, {}]  # CALLERROR message type
        if charger_id in self.chargers:
            await self.chargers[charger_id].send(json.dumps(error_message))
            print(f"[SERVER] Sent ERROR to {charger_id}: {json.dumps(error_message)}")

    async def handle_disconnect(self, charger_id):
        """Handle charger disconnection"""
        print(f"Charger disconnected: {charger_id}")
        if charger_id in self.chargers:
            del self.chargers[charger_id]

async def main():
    server = OCPPServer(9220)
    await server.start()

if __name__ == "__main__":
    asyncio.run(main())