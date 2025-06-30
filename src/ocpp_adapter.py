import asyncio
import websockets
import os
import sys
import json
from urllib.parse import urlparse, parse_qs
from pymodbus.server import StartTcpServer
from pymodbus.datastore import ModbusServerContext, ModbusSlaveContext, ModbusSequentialDataBlock
from pymodbus.device import ModbusDeviceIdentification
from websockets import State
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

class OCPPAdapter:
    def __init__(self, server_port, upstream_server_url, modbus_config, data_points):
        self.server_port = server_port
        self.upstream_server_url = upstream_server_url
        self.modbus_config = modbus_config
        self.data_points = data_points
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
            # # self.store.setValues(function_code, address, [modbus_value], unit=slave_id)
            # self.store.setValues(3, 0, [self.registers['energy']])
            # self.store.setValues(3, 1, [self.registers['soc']])
            # self.store.setValues(3, 2, [self.registers['status']])
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
            # query_params = parse_qs(urlparse(path).query)
            # charger_id = query_params.get('chargerId', [None])[0]
            
            # Extract chargerId from the URL path (e.g., /{chargerId})
            path_parts = path.strip('/').split('/')
            if len(path_parts) == 1:
                charger_id = path_parts[0]
            else:
                charger_id = None

            if not charger_id:
                print("[OCPP] No chargerId provided, closing connection")
                await websocket.close()
                return

            print(f"[OCPP] Charger connected: {charger_id}")
            
            # Connect to upstream server, passing charger_id as part of the URL
            upstream_url = f"{self.upstream_server_url}/{charger_id}"
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
        """Update Modbus registers from MeterValues message."""
        try:
            payload = message_data[3] if len(message_data) > 3 else None
            print(f"[OCPP] Processing MeterValues: {payload}")
            
            if payload and 'meterValue' in payload and len(payload['meterValue']) > 0:
                for meter_value in payload['meterValue']:
                    timestamp = meter_value.get('timestamp', None)
                    sampled_values = meter_value.get('sampledValue', [])
                    
                    print(f"[OCPP] Timestamp: {timestamp}, Sampled Values: {sampled_values}")

                    for sampled_value in sampled_values:
                        print(f"[OCPP] Sampled Value: {sampled_value}")
                        measurand = sampled_value.get('measurand', None)
                        value = sampled_value.get('value', None)

                        # print(f"data_point: {self.data_points} ")
                        # Cari data_point yang sesuai dengan measurand
                        for data_point in self.data_points:
                            # print(f"[Modbus] Checking data point: {data_point['ocpp_name']} for measurand: {measurand}")
                            if data_point['ocpp_name'] == measurand and data_point['is_active']:
                                function_code = data_point['function_code']
                                address = data_point['address']
                                data_type = data_point['data_type']

                                # Konversi nilai berdasarkan data_type
                                if data_type == "INT":
                                    modbus_value = int(float(value))
                                elif data_type == "FLOAT":
                                    modbus_value = float(value)
                                else:
                                    print(f"[Modbus] Unsupported data type: {data_type}")
                                    continue
                                
                                print(f"[Modbus] Processing - Address: {address}, Value: {modbus_value}, Function Code: {function_code}, Measurand: {measurand}")

                                # Kirim ke Modbus
                                self.store.setValues(function_code, address, [modbus_value])
                                print(f"[Modbus] Sent to Modbus - Address: {address}, Value: {modbus_value}, Function Code: {function_code}")
                            elif data_point['ocpp_name'] == measurand and not data_point['is_active']:
                                print(f"[Modbus] Skipped inactive data point: {data_point['ocpp_name']}")
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

class ConfigChangeHandler(FileSystemEventHandler):
    def __init__(self, restart_callback):
        self.restart_callback = restart_callback

    def on_modified(self, event):
        if event.src_path.endswith("database.json"):
            print("[Config] Detected change in database.json, restarting...")
            self.restart_callback()

def restart_application():
    """Restart the application by re-executing the script."""
    os.execv(sys.executable, ['python'] + sys.argv)

async def main():
    # Path ke file konfigurasi
    config_path = os.path.join(os.path.dirname(__file__), '..', '..', 'database', 'database.json')
    config_dir = os.path.dirname(config_path)

    server_port = None
    upstream_server_url = ""
    modbus_config = {}
    data_points = []

    # Baca konfigurasi dari database.json
    try:
        with open(config_path, 'r') as config_file:
            config = json.load(config_file)
        
        # Ambil nilai konfigurasi
        server_port = config.get('general').get('port')
        upstream_server_url = f"ws://{config.get('uplink_forward_to', {}).get('url_forward_to', '127.0.0.1')}:{config.get('uplink_forward_to', {}).get('port_forward_to', '9220')}"
        modbus_config = {
            'host': '0.0.0.0', # defualt host untuk Modbus
            'port': config.get('uplink_to', {}).get('port_uplink_to', 5020)
        }
        data_points = config.get('data_point', [])

        print(f"[Config] Loaded configuration: server_port={server_port}, upstream_server_url={upstream_server_url}, modbus_config={modbus_config}")
    except Exception as e:
        print(f"[Config] Error loading configuration: {e}")
        return

    # Inisialisasi adapter dengan konfigurasi yang dibaca
    adapter = OCPPAdapter(
        server_port= server_port,
        upstream_server_url= upstream_server_url,
        modbus_config= modbus_config,
        data_points= data_points
    )

    # Mulai pemantauan perubahan file
    event_handler = ConfigChangeHandler(restart_callback=restart_application)
    observer = Observer()
    observer.schedule(event_handler, path=config_dir, recursive=False)
    observer.start()

    try:
        # Jalankan server
        await adapter.start_server()
    finally:
        observer.stop()
        observer.join()

if __name__ == "__main__":
    asyncio.run(main())
