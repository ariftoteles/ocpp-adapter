import asyncio
import websockets
import json
from uuid import uuid4
from datetime import datetime, timezone
import random
from websockets import State

class EVCharger:
    def __init__(self, charger_id, server_url):
        self.charger_id = charger_id
        self.server_url = f"{server_url}/{charger_id}"
        self.transaction_id = None
        self.meter_interval = None
        self.pending_requests = {}
        self.is_connected = False
        self.ws = None
        self.is_charging_complete = False
        self.heartbeat_task = None
        
        asyncio.create_task(self.connect())

    async def connect(self):
        try:
            self.ws = await websockets.connect(self.server_url)
            self.is_connected = True
            print(f"[CLIENT {self.charger_id}] Connected to server")
            await self.on_connect()
            await self.listen_for_messages()
        except Exception as e:
            print(f"[CLIENT {self.charger_id}] Connection error: {e}")

    async def on_connect(self):
        # Step 1: Send BootNotification when connected
        await self.send_message('BootNotification', {
            'chargePointVendor': 'Tesla',
            'chargePointModel': 'Supercharger V3',
            'firmwareVersion': '1.0.0'
        })
        # Start sending Heartbeat messages
        # self.heartbeat_task = asyncio.create_task(self.send_heartbeat())

    # async def send_heartbeat(self):
    #     while self.is_connected:
    #         try:
    #             await self.send_message('Heartbeat', {})
    #             await asyncio.sleep(5)  # Send Heartbeat every 5 seconds
    #         except Exception as e:
    #             print(f"[CLIENT {self.charger_id}] Error sending Heartbeat: {e}")
    #             break
    #     self.heartbeat_task.cancel()

    async def listen_for_messages(self):
        try:
            async for message in self.ws:
                await self.handle_message(message)
        except websockets.exceptions.ConnectionClosed:
            print(f"[CLIENT {self.charger_id}] Disconnected from server")
        finally:
            self.is_connected = False
            if self.meter_interval:
                self.meter_interval.cancel()

    async def handle_message(self, data):
        try:
            message = json.loads(data)
            message_type = message[0]

            print(f"[CLIENT RECEIVED] from {self.charger_id}: {message}")

            if message_type == 4:  # Handle error
                _, message_id, error_code, error_desc = message[:4]
                print(f"[CLIENT {self.charger_id}] ERROR: {error_code} - {error_desc}")
                return

            if message_type == 3:  # Handle CALLRESULT
                _, message_id, payload = message[:3]
                request = self.pending_requests.get(message_id)
                
                if not request:
                    return

                self.pending_requests.pop(message_id)

                # Step 2: After BootNotification accepted, send Authorize
                if request['action'] == 'BootNotification':
                    await self.send_message('Authorize', {'idTag': 'TAG_123'})

                # Step 3: After Authorize success, send StartTransaction
                if (request['action'] == 'Authorize' and 
                    payload.get('idTagInfo', {}).get('status') == 'Accepted'):
                    await self.send_message('StartTransaction', {
                        'connectorId': 1,
                        'idTag': 'TAG_123',
                        'meterStart': 0,
                        'timestamp': datetime.now(timezone.utc).isoformat()
                    })

                # Step 4: After StartTransaction success, start sending MeterValues
                if request['action'] == 'StartTransaction':
                    self.transaction_id = payload['transactionId']
                    print(f"[CLIENT {self.charger_id}] Transaction started: {self.transaction_id}")

                    # Send MeterValues every 5 seconds
                    self.meter_interval = asyncio.create_task(self.send_meter_values())

                    # Stop after 30 seconds
                    await asyncio.sleep(30)
                    if self.meter_interval:
                        self.meter_interval.cancel()
                    await self.send_message('StopTransaction', {
                        'transactionId': self.transaction_id,
                        'idTag': 'TAG_123',
                        'meterStop': 5000,
                        'timestamp': datetime.now(timezone.utc).isoformat()
                    })

        except Exception as error:
            print(f"[CLIENT {self.charger_id}] Error: {error}")

    async def send_meter_values(self):
        while True:
            try:
                await self.send_message('MeterValues', {
                    'connectorId': 1,
                    'transactionId': self.transaction_id,
                    'meterValue': [{
                        'timestamp': datetime.now(timezone.utc).isoformat(),
                        'sampledValue': [{
                            'value': str(random.randint(0, 1000)),
                            'context': 'Sample.Periodic',
                            'format': 'Raw',
                            'measurand': 'Energy.Active.Import.Register',
                            'unit': 'Wh'
                        }]
                    }]
                })
                await asyncio.sleep(5)

            except Exception as e:
                print(f"[CLIENT {self.charger_id}] Error sending meter values: {e}")
                break

    async def send_message(self, action, payload):
        message_id = str(uuid4())
        message = [2, message_id, action, payload]
        
        if self.ws and self.ws.state == State.OPEN:  # Use the correct state check
            await self.ws.send(json.dumps(message))
            self.pending_requests[message_id] = {'action': action, 'payload': payload}
            print(f"[CLIENT {self.charger_id}] Sent {action}: {json.dumps(message)}")
        else:
            print(f"[CLIENT {self.charger_id}] Cannot send message - connection not open")

async def main():
    charger = EVCharger('CHARGER_001', 'ws://localhost:9221')
    # Keep the program running
    while True:
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(main())