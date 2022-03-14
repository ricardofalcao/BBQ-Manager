import asyncio
import json
from datetime import datetime

from PySide2.QtCore import QObject, Signal
from PySide2.QtWidgets import QLabel, QListWidgetItem
from bleak import BleakScanner, BleakClient
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

UART_SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
UART_CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
# All BLE devices have MTU of at least 23. Subtracting 3 bytes overhead, we can
# safely send 20 bytes at a time to any device supporting this service.
UART_SAFE_SIZE = 20


class Device(QObject):
    pass


class Scanner(QObject):
    pass


class Device(QObject):
    name: str
    client: BleakClient

    updated = Signal(Device)
    queued_commands = []
    pending_receives = {}

    list_widget: QListWidgetItem = None
    list_widget_label: QLabel = None

    dtime: datetime = datetime.min
    dtime_changed: bool = True

    alarm_time: datetime = datetime.min

    battery: int = 0
    firmware: str = 'unknown'

    settings = (0, 0)
    settings_changed: bool = True

    imu_acceleration = (0, 0, 0)
    imu_gyro = (0, 0, 0)

    def __init__(self, scanner: Scanner, ble: BLEDevice):
        QObject.__init__(self)

        self.scanner = scanner
        self.ble = ble
        self.name = ble.name
        self.read_buffer = ''
        self.running = False
        self.connected = False

    #
    #
    #

    async def _get_response(self, command: str):
        future = asyncio.Future()

        self.pending_receives[command] = future
        try:
            result = await asyncio.wait_for(future, 2.0)
            return result
        except TimeoutError:
            return None

    async def _send_cmd(self, command: str):
        if not self.running:
            return

        command = command + "\n"

        while len(command) > UART_SAFE_SIZE:
            await self.client.write_gatt_char(UART_CHAR_UUID, bytearray((command[0:UART_SAFE_SIZE]).encode()))
            await asyncio.sleep(0.2)
            command = command[UART_SAFE_SIZE:-1]

        if len(command) > 0:
            await self.client.write_gatt_char(UART_CHAR_UUID, bytearray((command + "\n").encode()))
            await asyncio.sleep(0.2)

    async def send_cmd(self, command: str):
        if not self.running:
            return

        self.queued_commands.append(command)

    #
    #
    #

    async def receive_cmd(self, data: str):
        split = data.split(":")
        command = split[0]
        print(f"Received: {data} {command}")

        if command == "ping":
            print("Sending pong")
            await self._send_cmd("pong")

            if not self.connected:
                self.scanner.device_connected.emit(self)
                self.connected = True
        elif command == "battery":
            self.battery = int(split[1])
        elif command == "firmware":
            self.firmware = split[1]
        elif command == "setsettings":
            if split[1] == "ok":
                await self.send_cmd("getsettings")

        self.updated.emit(self)

        if command in self.pending_receives:
            self.pending_receives[command].set_result(split[1] if len(split) > 1 else None)
            self.pending_receives.pop(command)

    #
    #
    #

    async def handle_rx(self, _: int, data: bytearray):
        command = data.decode()
        # print("RX: " + command + " - " + ("".join("{:02x} ".format(x) for x in data)))

        result = command.find('\n')

        while result != -1:
            self.read_buffer += command[0:result]

            try:
                await self.receive_cmd(self.read_buffer)
            except:
                pass

            self.read_buffer = ''

            command = command[result:-1]
            result = command.find('\n')

        self.read_buffer += command

    #
    #
    #

    def handle_disconnect(self, client: BleakClient):
        self.running = False
        self.scanner.blacklisted_ids.append(self.ble.address)
        print(f"[{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}] Device was disconnected, goodbye.")

        if self.ble.address in self.scanner.devices:
            self.scanner.device_disconnected.emit(self)
            del self.scanner.devices[self.ble.address]

    async def _sleep(self, _time: float):
        await asyncio.sleep(_time)

        if len(self.queued_commands) > 0:
            for i in range(len(self.queued_commands)):
                command = self.queued_commands[0]
                await self._send_cmd(command)
                self.queued_commands.remove(command)
                await asyncio.sleep(_time)

    async def run(self):
        tick = 0
        tick_duration = 0.5

        await self._send_cmd("getsettings")
        result = await self._get_response("getsettings")
        split2 = result[1].split(",")
        self.settings = (int(split2[0]), int(split2[1]))
        self.settings_changed = True

        while self.running:
            await self._send_cmd("gettime")
            result = await self._get_response("time")
            self.dtime = datetime.strptime(result[1], '%H,%M,%S,%d,%m,%y')
            self.dtime_changed = True

            await self._send_cmd("imudata")
            result = self._get_response("imudata")
            split2 = result[1].split(",")
            self.imu_acceleration = (float(split2[0]), float(split2[1]), float(split2[2]))
            self.imu_gyro = (float(split2[3]), float(split2[4]), float(split2[5]))

            if tick % 4 == 0:
                await self._send_cmd("battery")
                await self._sleep(tick_duration)

            if tick % 10 == 0:
                await self._send_cmd("firmware")
                await self._sleep(tick_duration)

            tick = tick + 1

    #
    #
    #

    async def connect_device(self):
        if self.running:
            return

        print(f"[{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}] Connecting device " + self.name)

        self.running = True
        self.client = BleakClient(self.ble, disconnected_callback=self.handle_disconnect, loop=asyncio.get_event_loop())

        self.updated.emit(self)

        try:
            await self.client.connect()
            await self.client.start_notify(UART_CHAR_UUID, self.handle_rx)

            await self.run()
        except:
            await self.disconnect_device()

    async def disconnect_device(self):
        if not self.running:
            return

        self.running = False

        self.scanner.device_disconnecting.emit(self)

        if self.client.is_connected:
            await self.client.disconnect()

        if self.ble.address in self.scanner.devices:
            self.scanner.device_disconnected.emit(self)
            del self.scanner.devices[self.ble.address]

class Scanner(QObject):
    devices = {}
    blacklisted_ids = []

    scanner: BleakScanner = None
    scanning = False

    scan_started = Signal()
    scan_finished = Signal()

    disconnect_started = Signal()
    disconnect_finished = Signal()

    device_connected = Signal(Device)
    device_disconnecting = Signal(Device)
    device_disconnected = Signal(Device)

    def __init__(self):
        QObject.__init__(self)

    async def device_found_callback(self, device: BLEDevice, adv: AdvertisementData):
        if device.address in self.devices:
            if device.name:
                ble_device = self.devices[device.address]
                ble_device.name = device.name
                ble_device.updated.emit(ble_device)

            return

        if UART_SERVICE_UUID.lower() not in adv.service_uuids:
            return

        if device.address in self.blacklisted_ids:
            return

        if device.name != 'BBQ3':
            return

        print("New device " + device.name + " - " + device.address)

        new_device = Device(self, device)
        self.devices[device.address] = new_device

        await new_device.connect_device()

    async def scan_ble_devices(self):
        if self.scanning:
            return

        print("Scanning for devices")
        self.blacklisted_ids.clear()

        self.scanning = True
        self.scan_started.emit()

        self.scanner = BleakScanner(detection_callback=self.device_found_callback)
        await self.scanner.start()

        print("Waiting...")
        await asyncio.sleep(30)

        print("Stopping scanner")
        await self.stop_ble_scan()

        print("Disposing unconnected devices")
        await self.disconnect_trash_devices()

        print("Finished scanning")

        self.scanning = False
        self.scan_finished.emit()

    #
    #
    #

    async def stop_ble_scan(self):
        if self.scanner:
            await self.scanner.stop()
            self.scanner = None

    #
    #
    #

    async def disconnect_ble_devices(self):
        self.disconnect_started.emit()
        await self.disconnect_devices()
        self.disconnect_finished.emit()

    async def disconnect_trash_devices(self):
        l = list(self.devices.values())

        for i in range(len(self.devices)):
            device = l[i]
            if not device.connected:
                await device.disconnect_device()

    async def disconnect_devices(self):
        l = list(self.devices.values())

        for i in range(len(self.devices)):
            device = l[0]
            await device.disconnect_device()