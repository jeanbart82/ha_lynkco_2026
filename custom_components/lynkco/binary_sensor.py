"""Binary sensor platform for Lynk & Co integration."""

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL_NAMES
from .coordinator import LynkCoCoordinator

BINARY_SENSOR_TYPES: list[dict] = [
    {"key": "door_front_left", "name": "Front left door", "field": "doorFrontLeftStatus", "device_class": BinarySensorDeviceClass.DOOR},
    {"key": "door_front_right", "name": "Front right door", "field": "doorFrontRightStatus", "device_class": BinarySensorDeviceClass.DOOR},
    {"key": "door_rear_left", "name": "Rear left door", "field": "doorRearLeftStatus", "device_class": BinarySensorDeviceClass.DOOR},
    {"key": "door_rear_right", "name": "Rear right door", "field": "doorRearRightStatus", "device_class": BinarySensorDeviceClass.DOOR},
    {"key": "window_front_left", "name": "Front left window", "field": "windowFrontLeftStatus", "device_class": BinarySensorDeviceClass.WINDOW},
    {"key": "window_front_right", "name": "Front right window", "field": "windowFrontRightStatus", "device_class": BinarySensorDeviceClass.WINDOW},
    {"key": "window_rear_left", "name": "Rear left window", "field": "windowRearLeftStatus", "device_class": BinarySensorDeviceClass.WINDOW},
    {"key": "window_rear_right", "name": "Rear right window", "field": "windowRearRightStatus", "device_class": BinarySensorDeviceClass.WINDOW},
    {"key": "sunroof", "name": "Sunroof", "field": "sunroofStatus", "device_class": BinarySensorDeviceClass.WINDOW, "exclude_models": ["E335"]},
    {"key": "hood", "name": "Hood", "field": "hoodStatus", "device_class": BinarySensorDeviceClass.DOOR},
    {"key": "trunk", "name": "Trunk", "field": "trunkStatus", "device_class": BinarySensorDeviceClass.DOOR},
    {"key": "tank_flap", "name": "Tank flap", "field": "tankFlapStatus", "device_class": BinarySensorDeviceClass.DOOR},
    {"key": "charge_lid", "name": "Charge lid", "field": "chargeLidStatus", "device_class": BinarySensorDeviceClass.DOOR},
    {"key": "doors_windows_closed", "name": "Doors and windows closed", "field": "generalStatus", "device_class": BinarySensorDeviceClass.DOOR},
]

VEHICLE_DATA_BINARY_SENSORS: list[dict] = [
    {"key": "car_running", "name": "Car running", "field": "driveModeEnabled", "device_class": BinarySensorDeviceClass.RUNNING, "icon": "mdi:car"},
    {"key": "telematics_enabled", "name": "Telematics enabled", "field": "vehicleTelematicsEnabled", "device_class": None, "icon": "mdi:access-point"},
    {"key": "location_data_enabled", "name": "Location data enabled", "field": "vehicleLocationDataEnabled", "device_class": None, "icon": "mdi:map-marker-check"},
    {"key": "location_sharing_enabled", "name": "Location sharing enabled", "field": "vehicleAutoShareLocationDataEnabled", "device_class": None, "icon": "mdi:map-marker-account"},
    {"key": "honking_permitted", "name": "Honking permitted", "field": "honkingPermitted", "device_class": None, "icon": "mdi:bullhorn"},
    {"key": "flashing_permitted", "name": "Flashing permitted", "field": "flashingPermitted", "device_class": None, "icon": "mdi:car-light-high"},
    {"key": "charging_data_accurate", "name": "Charging data accurate", "field": "isChargingDataAccurate", "device_class": None, "icon": "mdi:check-network", "data_key": "charge"},
    {"key": "climate_target_configurable", "name": "Climate target configurable", "field": "isTargetTemperatureConfigurable", "device_class": None, "icon": "mdi:thermometer-cog", "data_key": "climate"},
    {"key": "climate_usage_limited", "name": "Climate usage limited", "field": "isUsageLimited", "device_class": None, "icon": "mdi:timer-alert", "data_key": "climate"},
    {"key": "engine_started_low_battery", "name": "Engine started for low battery", "field": "engineStartingForLowBattery", "device_class": None, "icon": "mdi:battery-alert", "data_key": "climate"},
    {"key": "engine_started_when_active", "name": "Engine started when climate active", "field": "isEngineStartedWhenActive", "device_class": None, "icon": "mdi:engine", "data_key": "climate"},
]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for vin, coordinator in data["coordinators"].items():
        for sensor_type in BINARY_SENSOR_TYPES:
            if coordinator.model in sensor_type.get("exclude_models", []):
                continue
            entities.append(LynkCoBinarySensor(coordinator, sensor_type))
        for sensor_type in VEHICLE_DATA_BINARY_SENSORS:
            entities.append(LynkCoVehicleDataBinarySensor(coordinator, sensor_type))
    async_add_entities(entities)


class LynkCoBinarySensor(CoordinatorEntity, BinarySensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: LynkCoCoordinator, sensor_type: dict) -> None:
        super().__init__(coordinator)
        self._sensor_type = sensor_type
        self._attr_unique_id = f"{coordinator.vin}_{sensor_type['key']}"
        self._attr_translation_key = sensor_type["key"]
        self._attr_device_class = sensor_type["device_class"]

    @property
    def device_info(self):
        return self.coordinator.device_info

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        doors = self.coordinator.data.get("doors", {})
        value = doors.get(self._sensor_type["field"])
        if value is None:
            return None
        return value != "CLOSED"

    @property
    def available(self) -> bool:
        return super().available and not self.coordinator.endpoint_errors.get("doors")


class LynkCoVehicleDataBinarySensor(CoordinatorEntity, BinarySensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: LynkCoCoordinator, sensor_type: dict) -> None:
        super().__init__(coordinator)
        self._sensor_type = sensor_type
        self._attr_unique_id = f"{coordinator.vin}_{sensor_type['key']}"
        self._attr_translation_key = sensor_type["key"]
        self._attr_device_class = sensor_type["device_class"]
        if "icon" in sensor_type:
            self._attr_icon = sensor_type["icon"]

    @property
    def device_info(self):
        return self.coordinator.device_info

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        data_key = self._sensor_type.get("data_key", "vehicle_data")
        return self.coordinator.data.get(data_key, {}).get(self._sensor_type["field"])

    @property
    def available(self) -> bool:
        return super().available and not self.coordinator.endpoint_errors.get(
            self._sensor_type.get("data_key", "vehicle_data")
        )
