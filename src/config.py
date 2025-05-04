import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    OCPP_SERVER_PORT = int(os.getenv("OCPP_SERVER_PORT", 9221))
    UPSTREAM_SERVER_URL = os.getenv("UPSTREAM_SERVER_URL", "ws://localhost:9220")
    MODBUS_HOST = os.getenv("MODBUS_HOST", "0.0.0.0")
    MODBUS_PORT = int(os.getenv("MODBUS_PORT", 5020))