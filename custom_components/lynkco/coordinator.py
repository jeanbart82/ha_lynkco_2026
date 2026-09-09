"""Data coordinator for Lynk & Co integration."""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from datetime import datetime, timedelta
from typing import Any

from homeassistant.util import dt as dt_util

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import LynkCoAPI
from .const import (
    CLIMATE_SCAN_INTERVAL,
    CONF_DRIVING_INTERVAL,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DRIVING_SCAN_INTERVAL,
    DOMAIN,
    MANUFACTURER,
    MODEL_NAMES,
)

_LOGGER = logging.getLogger(__name__)

REFRESH_RETRY_DELAYS = [3, 5, 10]
FAST_POLL_BASE_INTERVAL = timedelta(seconds=CLIMATE_SCAN_INTERVAL)  # finest fast-poll cadence


class LynkCoCoordinator(DataUpdateCoordinator):
    """Fetch data from Lynk & Co API."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: LynkCoAPI,
        vin: str,
        model: str,
    ) -> None:
        scan_minutes = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL // 60)
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{vin}",
            update_interval=timedelta(minutes=scan_minutes),
        )
        self.api = api
        self.vin = vin
        self.model = model
        self.propulsion: str | None = None  # Set after first fetch (e.g. "PHEV", "BEV")
        self.entry = entry
        self._last_fast_poll: dict[str, datetime] = {}
        self.endpoint_errors: dict[str, str] = {}

    @property
    def device_info(self) -> dict[str, object]:
        """Return shared device metadata for every Lynk & Co entity."""
        vehicle = (self.data or {}).get("metadata", {}).get("vehicle") or {}
        return {
            "identifiers": {(DOMAIN, self.vin)},
            "name": MODEL_NAMES.get(self.model, f"Lynk & Co {self.model}"),
            "manufacturer": MANUFACTURER,
            "model": MODEL_NAMES.get(self.model, self.model),
            "serial_number": self.vin,
            "sw_version": vehicle.get("year"),
            "hw_version": vehicle.get("propulsionType"),
        }

    async def _async_fetch_all(self) -> dict:
        endpoints: dict[str, Callable[[], Coroutine[Any, Any, dict]]] = {
            "vehicle_data": lambda: self.api.get_vehicle_data(self.vin),
            "location": lambda: self.api.get_location(self.vin),
            "charge": lambda: self.api.get_charge_state(self.vin),
            "climate": lambda: self.api.get_climate_state(self.vin),
            "doors": lambda: self.api.get_doors_windows(self.vin),
            "metadata": lambda: self.api.get_vehicle_metadata(self.vin),
        }
        if self.propulsion != "BEV":
            endpoints["fuel"] = lambda: self.api.get_fuel_state(self.vin)

        old_data = self.data or {}
        # Keep requests sequential: the API client may refresh the shared token
        # after a 401, and concurrent refreshes could overwrite each other.
        results = [
            await self._fetch_endpoint(key, fetch_fn)
            for key, fetch_fn in endpoints.items()
        ]
        data = {key: value for key, value in results if value is not None}
        if not data and not old_data:
            raise UpdateFailed("All Lynk & Co API endpoints failed")

        # Keep the last known value for an endpoint that failed. Entities tied
        # to that endpoint are marked unavailable through endpoint_errors.
        snapshot = {
            key: data.get(key, old_data.get(key, {}))
            for key in ("vehicle_data", "location", "charge", "climate", "doors", "fuel", "metadata")
        }
        snapshot["last_updated"] = (
            dt_util.now() if data else old_data.get("last_updated")
        )
        snapshot["_endpoint_errors"] = dict(self.endpoint_errors)
        return snapshot

    async def _async_update_data(self) -> dict:
        data = await self._async_fetch_all()

        # Store propulsion type for entity filtering
        propulsion = (data["metadata"].get("vehicle") or {}).get("propulsionType")
        if propulsion:
            self.propulsion = propulsion

        return data

    async def _fetch_endpoint(
        self, key: str, fetch_fn: Callable[[], Coroutine[Any, Any, dict]]
    ) -> tuple[str, dict | None]:
        """Fetch one endpoint without taking unrelated entities offline."""
        try:
            value = await fetch_fn()
        except Exception as err:
            self.endpoint_errors[key] = str(err)
            _LOGGER.warning("Lynk & Co endpoint %s failed: %s", key, err)
            return key, None
        self.endpoint_errors.pop(key, None)
        return key, value

    def start_fast_poll(self) -> Callable[[], None]:
        """Start the endpoint-specific fast-poll timer; returns an unsub callable.

        The full snapshot keeps polling on ``update_interval`` as a backstop; this
        timer additionally refreshes only the endpoints relevant to whatever is
        currently active (driving / climate), each at its own cadence.
        """
        return async_track_time_interval(self.hass, self._async_fast_poll, FAST_POLL_BASE_INTERVAL)

    def _fast_poll_targets(self) -> list[tuple[str, str, timedelta]]:
        """(data_key, api_method, period) for each endpoint to fast-poll right now."""
        data = self.data or {}
        targets: list[tuple[str, str, timedelta]] = []
        if (data.get("vehicle_data") or {}).get("driveModeEnabled", False):
            drive = timedelta(
                minutes=self.entry.options.get(CONF_DRIVING_INTERVAL, DRIVING_SCAN_INTERVAL // 60)
            )
            # vehicle_data polls at the base cadence so drive-end is detected promptly
            targets.append(("vehicle_data", "get_vehicle_data", FAST_POLL_BASE_INTERVAL))
            targets.append(("location", "get_location", drive))
            targets.append(("charge", "get_charge_state", drive))
        status = (data.get("climate") or {}).get("status")
        if str(status or "").upper().startswith("ACTIVE"):
            targets.append(("climate", "get_climate_state", FAST_POLL_BASE_INTERVAL))
        return targets

    async def _async_fast_poll(self, now: datetime) -> None:
        """Refresh only the endpoints relevant to the active trigger(s)."""
        if self.data is None:
            return

        slack = timedelta(seconds=1)  # tolerate timer jitter so a 60s target fires each 60s tick
        due: dict[str, str] = {}
        for key, fn_name, period in self._fast_poll_targets():
            last = self._last_fast_poll.get(key)
            if last is None or (now - last) >= (period - slack):
                due[key] = fn_name
        if not due:
            return

        was_driving = (self.data.get("vehicle_data") or {}).get("driveModeEnabled", False)
        updates: dict[str, Any] = {}
        errors_before = dict(self.endpoint_errors)
        for key, fn_name in due.items():
            try:
                updates[key] = await getattr(self.api, fn_name)(self.vin)
                self._last_fast_poll[key] = now
                self.endpoint_errors.pop(key, None)
            except Exception as err:
                self.endpoint_errors[key] = str(err)
                _LOGGER.warning("Fast poll of Lynk & Co endpoint %s failed: %s", key, err)
        if not updates:
            if self.endpoint_errors != errors_before:
                self.data = {
                    **self.data,
                    "_endpoint_errors": dict(self.endpoint_errors),
                }
                self.async_update_listeners()
            return

        changed = any(updates[k] != self.data.get(k) for k in updates)
        self.data = {
            **self.data,
            **updates,
            "last_updated": dt_util.now(),
            "_endpoint_errors": dict(self.endpoint_errors),
        }
        if changed or updates:
            self.async_update_listeners()
        _LOGGER.debug("Fast-polled %s for %s (changed=%s)", list(updates), self.vin, changed)

        # LynkOS 1.4.0+ only refreshes location when the car stops, so when
        # driving has just ended, chase the final (destination) location.
        now_driving = (updates.get("vehicle_data") or {}).get("driveModeEnabled", False)
        if was_driving and "vehicle_data" in updates and not now_driving:
            self.hass.async_create_task(
                self.async_targeted_refresh("location", lambda: self.api.get_location(self.vin))
            )

    async def async_targeted_refresh(
        self,
        data_key: str,
        fetch_fn: Callable[[], Coroutine[Any, Any, dict]],
    ) -> None:
        """Refresh a single data key with retry logic."""
        if self.data is None:
            return

        old_value = self.data.get(data_key)

        for delay in REFRESH_RETRY_DELAYS:
            await asyncio.sleep(delay)
            had_error = data_key in self.endpoint_errors
            try:
                new_value = await fetch_fn()
            except Exception as err:
                self.endpoint_errors[data_key] = str(err)
                _LOGGER.debug("Targeted refresh of %s failed, will retry", data_key)
                continue
            self.endpoint_errors.pop(data_key, None)

            if new_value != old_value:
                self.data = {
                    **self.data,
                    data_key: new_value,
                    "last_updated": dt_util.now(),
                    "_endpoint_errors": dict(self.endpoint_errors),
                }
                self.async_update_listeners()
                _LOGGER.debug("Targeted refresh of %s detected change", data_key)
                return
            if had_error:
                self.data = {
                    **self.data,
                    "last_updated": dt_util.now(),
                    "_endpoint_errors": dict(self.endpoint_errors),
                }
                self.async_update_listeners()
                return

        _LOGGER.debug("Targeted refresh of %s: no change after %d retries", data_key, len(REFRESH_RETRY_DELAYS))
        if self.endpoint_errors.get(data_key) is not None:
            self.data = {
                **self.data,
                "_endpoint_errors": dict(self.endpoint_errors),
            }
            self.async_update_listeners()
