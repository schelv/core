"""Flux for Home-Assistant.

The idea was taken from https://github.com/KpaBap/hue-flux/
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP,
    ATTR_RGB_COLOR,
    ATTR_TRANSITION,
    ATTR_XY_COLOR,
    DOMAIN as LIGHT_DOMAIN,
    VALID_TRANSITION,
    is_on,
)
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_BRIGHTNESS,
    CONF_LIGHTS,
    CONF_MODE,
    CONF_NAME,
    CONF_PLATFORM,
    SERVICE_TURN_ON,
    STATE_ON,
    SUN_EVENT_SUNRISE,
    SUN_EVENT_SUNSET,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    DOMAIN as HOMEASSISTANT_DOMAIN,
    HomeAssistant,
)
from homeassistant.helpers import config_validation as cv, entity_platform, event
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.issue_registry import IssueSeverity, async_create_issue
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.util.color import (
    color_RGB_to_xy_brightness,
    color_temperature_kelvin_to_mired,
    color_temperature_to_rgb,
)
from homeassistant.util.dt import as_local, parse_time, utcnow as dt_utcnow

from .const import (
    CONF_ADJUST_BRIGHTNESS,
    CONF_INTERVAL,
    CONF_START_CT,
    CONF_START_TIME,
    CONF_STOP_CT,
    CONF_STOP_TIME,
    CONF_SUNSET_CT,
    DEFAULT_MODE,
    DOMAIN,
    MODE_MIRED,
    MODE_RGB,
    MODE_XY,
)

_LOGGER = logging.getLogger(__name__)

CONF_DISABLE_BRIGHTNESS_ADJUST = "disable_brightness_adjust"

PLATFORM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PLATFORM): "flux",
        vol.Required(CONF_LIGHTS): cv.entity_ids,
        vol.Optional(CONF_NAME, default="Flux"): cv.string,
        vol.Optional(CONF_START_TIME): cv.time,
        vol.Optional(CONF_STOP_TIME): cv.time,
        vol.Optional(CONF_START_CT, default=4000): vol.All(
            vol.Coerce(int), vol.Range(min=1000, max=40000)
        ),
        vol.Optional(CONF_SUNSET_CT, default=3000): vol.All(
            vol.Coerce(int), vol.Range(min=1000, max=40000)
        ),
        vol.Optional(CONF_STOP_CT, default=1900): vol.All(
            vol.Coerce(int), vol.Range(min=1000, max=40000)
        ),
        vol.Optional(CONF_BRIGHTNESS): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=255)
        ),
        vol.Optional(CONF_DISABLE_BRIGHTNESS_ADJUST): cv.boolean,
        vol.Optional(CONF_MODE, default=DEFAULT_MODE): vol.Any(
            MODE_XY, MODE_MIRED, MODE_RGB
        ),
        vol.Optional(CONF_INTERVAL, default=30): cv.positive_int,
        vol.Optional(ATTR_TRANSITION, default=30): VALID_TRANSITION,
    }
)


async def async_set_lights_xy(
    hass: HomeAssistant, lights, x_val, y_val, brightness, transition: timedelta
):
    """Set color of array of lights."""
    for light in lights:
        if is_on(hass, light):
            service_data = {ATTR_ENTITY_ID: light}
            if x_val is not None and y_val is not None:
                service_data[ATTR_XY_COLOR] = [x_val, y_val]
            if brightness is not None:
                service_data[ATTR_BRIGHTNESS] = brightness
            if transition is not None:
                service_data[ATTR_TRANSITION] = transition.total_seconds()
            await hass.services.async_call(LIGHT_DOMAIN, SERVICE_TURN_ON, service_data)


async def async_set_lights_temp(
    hass: HomeAssistant, lights, mired, brightness, transition: timedelta
):
    """Set color of array of lights."""
    for light in lights:
        if is_on(hass, light):
            service_data = {ATTR_ENTITY_ID: light}
            if mired is not None:
                service_data[ATTR_COLOR_TEMP] = int(mired)
            if brightness is not None:
                service_data[ATTR_BRIGHTNESS] = brightness
            if transition is not None:
                service_data[ATTR_TRANSITION] = transition.total_seconds()
            await hass.services.async_call(LIGHT_DOMAIN, SERVICE_TURN_ON, service_data)


async def async_set_lights_rgb(hass: HomeAssistant, lights, rgb, transition: timedelta):
    """Set color of array of lights."""
    for light in lights:
        if is_on(hass, light):
            service_data = {ATTR_ENTITY_ID: light}
            if rgb is not None:
                service_data[ATTR_RGB_COLOR] = rgb
            if transition is not None:
                service_data[ATTR_TRANSITION] = transition.total_seconds()
            await hass.services.async_call(LIGHT_DOMAIN, SERVICE_TURN_ON, service_data)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Old method of loading Flux from yaml configuration."""

    async_create_issue(
        hass,
        HOMEASSISTANT_DOMAIN,
        f"deprecated_yaml_{DOMAIN}",
        breaks_in_ha_version="2024.11.0",
        is_fixable=False,
        issue_domain=DOMAIN,
        severity=IssueSeverity.WARNING,
        translation_key="deprecated_yaml",
        translation_placeholders={
            "domain": DOMAIN,
            "integration_title": "Flux",
        },
    )

    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_IMPORT}, data=config
        )
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: config_entries.ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Flux lights."""
    name = entry.options[CONF_NAME]
    lights = entry.options[CONF_LIGHTS]

    start_time = parse_time(entry.options.get(CONF_START_TIME))  # type: ignore[arg-type]
    stop_time = parse_time(entry.options.get(CONF_STOP_TIME))  # type: ignore[arg-type]

    start_colortemp = entry.options[CONF_START_CT]
    sunset_colortemp = entry.options[CONF_SUNSET_CT]
    stop_colortemp = entry.options[CONF_STOP_CT]

    brightness = None
    disable_brightness_adjust = not entry.options[CONF_ADJUST_BRIGHTNESS]
    mode = entry.options[CONF_MODE]
    interval = timedelta(**entry.options[CONF_INTERVAL])
    transition = timedelta(**entry.options[ATTR_TRANSITION])

    flux = FluxSwitch(
        name,
        hass,
        lights,
        start_time,
        stop_time,
        start_colortemp,
        sunset_colortemp,
        stop_colortemp,
        brightness,
        disable_brightness_adjust,
        mode,
        interval,
        transition,
    )

    async_add_entities([flux])

    platform = entity_platform.async_get_current_platform()

    async def async_update() -> None:
        """Update lights."""
        await flux.async_flux_update()

    platform.async_register_entity_service(
        "flux_update",
        {},
        async_update,
    )


class FluxSwitch(SwitchEntity, RestoreEntity):
    """Representation of a Flux switch."""

    def __init__(
        self,
        name: str,
        hass: HomeAssistant,
        lights,
        start_time: time | None,
        stop_time: time | None,
        start_colortemp,
        sunset_colortemp,
        stop_colortemp,
        brightness,
        disable_brightness_adjust,
        mode,
        interval: timedelta,
        transition: timedelta,
    ) -> None:
        """Initialize the Flux switch."""
        self._name = name
        self.hass = hass
        self._lights = lights
        self._start_time = start_time
        self._stop_time = stop_time
        self._start_colortemp = start_colortemp
        self._sunset_colortemp = sunset_colortemp
        self._stop_colortemp = stop_colortemp
        self._brightness = brightness
        self._disable_brightness_adjust = disable_brightness_adjust
        self._mode = mode
        self._interval = interval
        self._transition = transition
        self.unsub_tracker: CALLBACK_TYPE | None = None

    @property
    def name(self):
        """Return the name of the device if any."""
        return self._name

    @property
    def is_on(self):
        """Return true if switch is on."""
        return self.unsub_tracker is not None

    async def async_added_to_hass(self) -> None:
        """Call when entity about to be added to hass."""
        last_state = await self.async_get_last_state()
        if last_state and last_state.state == STATE_ON:
            await self.async_turn_on()

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity will be removed from hass."""
        if self.unsub_tracker:
            self.unsub_tracker()
        return await super().async_will_remove_from_hass()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on flux."""
        if self.is_on:
            return

        self.unsub_tracker = event.async_track_time_interval(
            self.hass, self.async_flux_update, self._interval
        )

        # Make initial update
        await self.async_flux_update()

        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off flux."""
        if self.unsub_tracker is not None:
            self.unsub_tracker()
            self.unsub_tracker = None

        self.async_write_ha_state()

    async def async_flux_update(self, utcnow=None):
        """Update all the lights using flux."""
        if utcnow is None:
            utcnow = dt_utcnow()

        now = as_local(utcnow)

        sunset = get_astral_event_date(self.hass, SUN_EVENT_SUNSET, now.date())
        start_time = self.find_start_time(now)
        stop_time = self.find_stop_time(now)

        if stop_time <= start_time:
            # stop_time does not happen in the same day as start_time
            if start_time < now:
                # stop time is tomorrow
                stop_time += timedelta(days=1)
        elif now < start_time:
            # stop_time was yesterday since the new start_time is not reached
            stop_time -= timedelta(days=1)

        if start_time < now < sunset:
            # Daytime
            time_state = "day"
            temp_range = abs(self._start_colortemp - self._sunset_colortemp)
            day_length = int(sunset.timestamp() - start_time.timestamp())
            seconds_from_start = int(now.timestamp() - start_time.timestamp())
            percentage_complete = seconds_from_start / day_length
            temp_offset = temp_range * percentage_complete
            if self._start_colortemp > self._sunset_colortemp:
                temp = self._start_colortemp - temp_offset
            else:
                temp = self._start_colortemp + temp_offset
        else:
            # Night time
            time_state = "night"

            if now < stop_time:
                if stop_time < start_time and stop_time.day == sunset.day:
                    # we need to use yesterday's sunset time
                    sunset_time = sunset - timedelta(days=1)
                else:
                    sunset_time = sunset

                night_length = int(stop_time.timestamp() - sunset_time.timestamp())
                seconds_from_sunset = int(now.timestamp() - sunset_time.timestamp())
                percentage_complete = seconds_from_sunset / night_length
            else:
                percentage_complete = 1

            temp_range = abs(self._sunset_colortemp - self._stop_colortemp)
            temp_offset = temp_range * percentage_complete
            if self._sunset_colortemp > self._stop_colortemp:
                temp = self._sunset_colortemp - temp_offset
            else:
                temp = self._sunset_colortemp + temp_offset
        rgb = color_temperature_to_rgb(temp)
        x_val, y_val, b_val = color_RGB_to_xy_brightness(*rgb)
        brightness = self._brightness if self._brightness else b_val
        if self._disable_brightness_adjust:
            brightness = None
        if self._mode == MODE_XY:
            await async_set_lights_xy(
                self.hass, self._lights, x_val, y_val, brightness, self._transition
            )
            _LOGGER.debug(
                (
                    "Lights updated to x:%s y:%s brightness:%s, %s%% "
                    "of %s cycle complete at %s"
                ),
                x_val,
                y_val,
                brightness,
                round(percentage_complete * 100),
                time_state,
                now,
            )
        elif self._mode == MODE_RGB:
            await async_set_lights_rgb(self.hass, self._lights, rgb, self._transition)
            _LOGGER.debug(
                "Lights updated to rgb:%s, %s%% of %s cycle complete at %s",
                rgb,
                round(percentage_complete * 100),
                time_state,
                now,
            )
        else:
            # Convert to mired and clamp to allowed values
            mired = color_temperature_kelvin_to_mired(temp)
            await async_set_lights_temp(
                self.hass, self._lights, mired, brightness, self._transition
            )
            _LOGGER.debug(
                (
                    "Lights updated to mired:%s brightness:%s, %s%% "
                    "of %s cycle complete at %s"
                ),
                mired,
                brightness,
                round(percentage_complete * 100),
                time_state,
                now,
            )

    def find_start_time(self, now: datetime):
        """Return sunrise or start_time if given."""
        if self._start_time:
            sunrise = datetime.combine(now.date(), self._start_time, now.tzinfo)
        else:
            sunrise = get_astral_event_date(self.hass, SUN_EVENT_SUNRISE, now.date())  # type: ignore[assignment]
        return sunrise

    def find_stop_time(self, now: datetime):
        """Return dusk or stop_time if given."""
        if self._stop_time:
            dusk = datetime.combine(now.date(), self._stop_time, now.tzinfo)
        else:
            dusk = get_astral_event_date(self.hass, "dusk", now.date())  # type: ignore[assignment]
        return dusk
