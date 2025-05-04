import asyncio
import websockets
from urllib.parse import urlparse, parse_qs
import json
from pymodbus.server import StartTcpServer
from pymodbus.datastore import ModbusServerContext, ModbusSlaveContext, ModbusSequentialDataBlock
from pymodbus.device import ModbusDeviceIdentification
from websockets import State

class OCPPAdapter:
    def __init__(self, server_port, upstream_server_url, modbus_config):
        self.server_port = server_port
        self.upstream_server_url = upstream_server_url
        self.modbus_config = modbus_config
        self.message_queues = {}
        self.registers = {
            'energy': 0,
            'soc': 0,
            'status': 1
        }
        self.setup_modbus_server()
        print(f"OCPP Adapter running. Modbus TCP on {modbus_config['host']}:{modbus_config['port']}")

    def setup_modbus_server(self):
        def run_modbus_server():
            self.store = ModbusSlaveContext(
                hr=ModbusSequentialDataBlock(0, [0] * 10))
            self.store.setValues(3, 0, [self.registers['energy']])
            self.store.setValues(3, 1, [self.registers['soc']])
            self.store.setValues(3, 2, [self.registers['status']])
            context = ModbusServerContext(slaves=self.store, single=True)
            StartTcpServer(
                context=context,
                identity=self.get_modbus_identity(),
                address=(self.modbus_config['host'], self.modbus_config['port'])
            )
        
        import threading
        modbus_thread = threading.Thread(target=run_modbus_server, daemon=True)
        modbus_thread.start()

    def get_modbus_identity(self):
        identity = ModbusDeviceIdentification()
        identity.VendorName = 'OCPP Adapter'
        identity.ProductCode = 'OCPP-MODBUS'
        identity.VendorUrl = 'https://github.com/ariftoteles'
        identity.ProductName = 'OCPP to Modbus Adapter'
        identity.ModelName = 'OCPP-MODBUS-ADAPTER'
        identity.MajorMinorRevision = '1.0.0'
        return identity

    async def handle_connection(self, websocket, path=None):
        """Handle incoming WebSocket connections"""
        try:
            # Extract path if it's not provided directly (newer websockets versions)
            if path is None:
                if hasattr(websocket, "path"):
                    path = websocket.path
                elif hasattr(websocket, "request") and hasattr(websocket.request, "path"):
                    path = websocket.request.path
                elif hasattr(websocket, "request_headers") and "path" in websocket.request_headers:
                    path = websocket.request_headers["path"]
                else:
                    path = ""
                    print("Warning: Could not determine path from websocket object")
            
            # Extract chargerId from query parameters
            query_params = parse_qs(urlparse(path).query)
            charger_id = query_params.get('chargerId', [None])[0]
            
            if not charger_id:
                print("[OCPP] No chargerId provided, closing connection")
                await websocket.close()
                return

            print(f"[OCPP] Charger connected: {charger_id}")
            
            # Connect to upstream server, passing charger_id as part of the URL
            upstream_url = f"{self.upstream_server_url}?chargerId={charger_id}"
            upstream_ws = await websockets.connect(upstream_url)

            # Send initial message with charger_id to upstream server (optional, depending on how the server works)
            init_message = json.dumps({"charger_id": charger_id})
            await upstream_ws.send(init_message)
            print(f"[OCPP] Sent charger_id {charger_id} to upstream server")

            self.message_queues[charger_id] = []

            try:
                # Forward messages in both directions
                await asyncio.gather(
                    self.forward_messages(websocket, upstream_ws, charger_id),
                    self.forward_messages(upstream_ws, websocket, charger_id)
                )
            except websockets.exceptions.ConnectionClosed:
                print(f"[OCPP] Connection closed for charger: {charger_id}")
            finally:
                if charger_id in self.message_queues:
                    del self.message_queues[charger_id]
                await upstream_ws.close()

        except Exception as e:
            print(f"[OCPP] Connection handler error: {e}")

    async def forward_messages(self, source, destination, charger_id):
        """Forward messages from source to destination"""
        async for message in source:
            try:
                message_data = json.loads(message)
                
                # Handle StopTransaction action
                if len(message_data) > 2 and message_data[2] == 'StopTransaction':
                    self.registers['status'] = 0  # Ubah status menjadi 0
                    self.store.setValues(3, 2, [self.registers['status']])  # Update Modbus register
                    print(f"[Modbus] Status changed to 0 for charger: {charger_id}")
                
                if len(message_data) > 2 and message_data[2] == 'MeterValues':
                    self.process_meter_values(message_data)
                
                if destination.state == State.OPEN:
                    await destination.send(message)
                else:
                    self.message_queues[charger_id].append(message)
            except json.JSONDecodeError as e:
                print(f"[OCPP] Error decoding message: {e}")

    def process_meter_values(self, message_data):
        """Update Modbus registers from MeterValues message"""
        try:
            # Pastikan ada payload dengan MeterValues
            payload = message_data[3] if len(message_data) > 3 else None
            if payload and 'meterValue' in payload and len(payload['meterValue']) > 0:
                # Ambil data sampel pertama
                sampled_values = payload['meterValue'][0].get('sampledValue', [])
                for value in sampled_values:
                    # Cek apakah nilai yang diterima adalah untuk 'Energy' atau 'SoC'
                    # Tentukan logika jika tidak ada 'measurand'
                    if 'Wh' in value.get('unit', ''):  # Energi dalam Wh
                        self.registers['energy'] = int(float(value['value']))

                    elif 'SoC' in value.get('unit', ''):  # Jika unit adalah SoC, update SoC
                        self.registers['soc'] = int(float(value['value']))

                    else:
                        print(f"[OCPP] Unrecognized unit or measurand in MeterValues: {value}")
                    
                    self.store.setValues(3, 0, [self.registers['energy']])
                    self.store.setValues(3, 1, [self.registers['soc']])

                    # Tampilkan update ke register Modbus
                    print(f"[Modbus] Registers updated - Energy: {self.registers['energy']}, SoC: {self.registers['soc']}")
            else:
                print("[OCPP] No meterValue found in message.")
        except Exception as e:
            print(f"[OCPP] Error processing MeterValues: {e}")


    async def start_server(self):
        """Start the WebSocket server"""
        # Use websockets.serve with the correct handler
        self.server = await websockets.serve(
            self.handle_connection,
            "0.0.0.0", 
            self.server_port
        )
        print(f"OCPP Server running on port {self.server_port}")
        await self.server.wait_closed()

async def main():
    adapter = OCPPAdapter(
        server_port=9221,
        upstream_server_url='ws://localhost:9220',
        modbus_config={
            'host': '0.0.0.0',
            'port': 5020
        }
    )
    await adapter.start_server()

if __name__ == "__main__":
    asyncio.run(main())