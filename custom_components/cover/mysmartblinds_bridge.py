"""
Support for MySmartBlinds Smart Bridge

For more details about this component, please refer to the documentation at
https://home-assistant.io/components/cover.mysmartblinds_bridge
"""

import asyncio
import logging
from contextlib import contextmanager

import voluptuous as vol
from datetime import timedelta, datetime

from collections import defaultdict

from homeassistant.const import (
    CONF_PASSWORD, CONF_USERNAME, ATTR_BATTERY_LEVEL)
from homeassistant.components.cover import (
    ATTR_TILT_POSITION, CoverDevice,
    PLATFORM_SCHEMA, SUPPORT_CLOSE_TILT,
    SUPPORT_OPEN_TILT, SUPPORT_SET_TILT_POSITION,
    SUPPORT_SET_POSITION, SUPPORT_OPEN, SUPPORT_CLOSE,
    ATTR_POSITION)
from homeassistant.components.cover.group import CoverGroup
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity import generate_entity_id
from homeassistant.helpers.event import track_point_in_utc_time, track_time_interval
from homeassistant.util import Throttle, utcnow
from functools import wraps

REQUIREMENTS = ['smartblinds-client==0.2']

_LOGGER = logging.getLogger(__name__)

CONF_INCLUDE_ROOMS = 'include_rooms'

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_USERNAME): cv.string,
    vol.Required(CONF_PASSWORD): cv.string,
    vol.Optional(CONF_INCLUDE_ROOMS, default=True): cv.boolean,
})

POSITION_BATCHING_DELAY_SEC = 0.25
POLLING_INTERVAL_MINUTES = 5

EVENT_ENTITIES_ADDED = 'mysmartblinds_bridge.ready'

ATTR_RSSI_LEVEL = 'rssi_level'

DEBUG_DONT_MOVE = False


def timed(func):
    @contextmanager
    def timing():
        start_ts = datetime.now()
        yield
        elapsed = datetime.now() - start_ts
        _LOGGER.debug('{}() took {:.2f} seconds'.format(func.__name__, elapsed.total_seconds()))

    @wraps(func)
    def wrapper(*args, **kwargs):
        if not asyncio.iscoroutinefunction(func):
            with timing():
                return func(*args, **kwargs)
        else:
            async def async_wrapper():
                with timing():
                    return await func(*args, **kwargs)

            return async_wrapper()

    return wrapper


def setup_platform(hass, config, add_entities, discovery_info=None):
    """ Locate all available blinds """
    username = config[CONF_USERNAME]
    password = config[CONF_PASSWORD]
    include_rooms = config[CONF_INCLUDE_ROOMS]

    bridge = MySmartBlindsBridge(hass, username, password)

    blinds, rooms = bridge.get_blinds_and_rooms()

    entities = [BridgedMySmartBlindCover(
        bridge,
        blind,
        generate_entity_id('cover.{}', blind.name, hass=hass))
        for blind in blinds]

    if include_rooms:
        entities += [CoverGroup(
            room.name,
            list(map(
                lambda entity: entity.entity_id,
                filter(lambda entity: entity._blind.room_id == room.uuid, entities))))
            for room in rooms]

    add_entities(entities)
    bridge.entities = entities

    hass.bus.fire(EVENT_ENTITIES_ADDED)


class MySmartBlindsBridge:
    def __init__(self, hass, username, password):
        from smartblinds_client import SmartBlindsClient
        self._hass = hass
        self._sbclient = SmartBlindsClient(username=username, password=password)
        self._blinds = []
        self._rooms = []
        self._blind_states = {}
        self._blinds_by_mac = {}
        self._pending_blind_positions = {}
        self._cancel_pending_blind_position_timer = None
        self.entities = []

        track_time_interval(hass, self._update_periodic, timedelta(minutes=POLLING_INTERVAL_MINUTES))
        track_point_in_utc_time(hass, self._update_periodic, utcnow() + timedelta(seconds=10))

    @timed
    def get_blinds_and_rooms(self):
        try:
            self._sbclient.login()
            self._blinds, self._rooms = self._sbclient.get_blinds_and_rooms()
            self._blinds_by_mac.clear()
            for blind in self._blinds:
                self._blinds_by_mac[blind.encoded_mac] = blind

            return self._blinds, self._rooms
        except Exception as ex:
            _LOGGER.error("Error logging in or listing devices %s", ex)
            raise

    def get_blind_state(self, blind):
        if blind.encoded_mac in self._blind_states:
            state = self._blind_states[blind.encoded_mac]
            if state.position == -1:
                return None
            else:
                return state
        else:
            return None

    def set_blind_position(self, blind, position):
        self._pending_blind_positions[blind.encoded_mac] = position
        if self._cancel_pending_blind_position_timer is not None:
            self._cancel_pending_blind_position_timer()
        self._cancel_pending_blind_position_timer = track_point_in_utc_time(
            self._hass, self._set_blind_positions,
            utcnow() + timedelta(seconds=POSITION_BATCHING_DELAY_SEC))

    @timed
    def update_blind_states(self):
        from requests.exceptions import HTTPError

        try:
            self._blind_states = self._sbclient.get_blinds_state(self._blinds)
            _LOGGER.debug("Blind states: %s", self._blind_states)
        except HTTPError as http_error:
            if http_error.response.status_code == 401:
                self._sbclient.login()
                self.update_blind_states()
            else:
                raise

    @timed
    def _set_blind_positions(self, time=None):
        self._cancel_pending_blind_position_timer = None
        from requests.exceptions import HTTPError

        positions_blinds = defaultdict(list)
        for blind, position in self._pending_blind_positions.items():
            positions_blinds[position].append(blind)
        self._pending_blind_positions.clear()

        try:
            for position, blind_macs in positions_blinds.items():
                _LOGGER.debug("Moving %s to %d", ', '.join(blind_macs), position)

                blinds = [self._blinds_by_mac[blind] for blind in blind_macs]

                if DEBUG_DONT_MOVE:
                    new_blind_states = self._sbclient.get_blinds_state(blinds)
                else:
                    new_blind_states = self._sbclient.set_blinds_position(blinds, position)

                self._blind_states.update(new_blind_states)
            _LOGGER.debug("Blind states: %s", self._blind_states)
            for entity in self.entities:
                entity.schedule_update_ha_state(force_refresh=True)
        except HTTPError as http_error:
            if http_error.response.status_code == 401:
                self._sbclient.login()
                return self._set_blind_positions()
            else:
                raise

    @timed
    def _update_periodic(self, time=None):
        try:
            self.update_blind_states()
            for entity in self.entities:
                entity.schedule_update_ha_state(force_refresh=True)
        except Exception as ex:
            _LOGGER.error("Error updating periodic state %s", ex)


class BridgedMySmartBlindCover(CoverDevice):
    """
    A single MySmartBlinds cover, accessed through the Smart Bridge.
    """

    def __init__(self, bridge, blind, entity_id=None):
        """Init the device."""
        self._bridge = bridge
        self._blind = blind
        self._position = None
        self._available = False
        self._battery_level = 0
        self._rssi = None

        self.entity_id = entity_id

    @property
    def name(self):
        """Return the name of the blind."""
        return self._blind.name

    @property
    def should_poll(self):
        """Return the polling state."""
        return False

    @property
    def device_class(self):
        """Return the class of this device, from component DEVICE_CLASSES."""
        return 'window'

    @property
    def supported_features(self):
        """Flag supported features."""
        return SUPPORT_OPEN_TILT | SUPPORT_CLOSE_TILT | SUPPORT_SET_TILT_POSITION | \
               SUPPORT_SET_POSITION | SUPPORT_OPEN | SUPPORT_CLOSE

    @property
    def available(self):
        return self._available

    @property
    def is_closed(self):
        """Return true if cover is closed, else False."""
        if self._position is None:
            return None

        return self._position >= 190 or self._position <= 10

    @property
    def current_cover_tilt_position(self):
        """Return current position of cover tilt.
        None is unknown, 0 is closed, 100 is fully open.
        """
        if self._position is None:
            return None

        if self._position >= 190:
            rounded_position = 200
        elif self._position <= 10:
            rounded_position = 0
        else:
            rounded_position = self._position

        return int(100 - abs(rounded_position - 100))

    @property
    def current_cover_position(self):
        return self.current_cover_tilt_position

    @property
    def device_state_attributes(self):
        """Return the state attributes."""
        attr = {
            ATTR_BATTERY_LEVEL: self._battery_level,
            ATTR_RSSI_LEVEL: self._rssi,
        }
        return attr

    def open_cover_tilt(self, **kwargs):
        """Open the cover tilt."""
        self._tilt(100)

    def close_cover_tilt(self, **kwargs):
        """Close the cover"""
        self._tilt(200)

    def set_cover_tilt_position(self, **kwargs):
        """Move the cover tilt to a specific position."""
        self._tilt(200 - kwargs[ATTR_TILT_POSITION])

    def close_cover(self, **kwargs):
        return self.close_cover_tilt()

    def open_cover(self, **kwargs):
        return self.open_cover_tilt()

    def set_cover_position(self, **kwargs):
        """Move the cover to a specific position."""
        self._tilt(200 - kwargs[ATTR_POSITION])

    def update(self):
        state = self._bridge.get_blind_state(self._blind)
        if state is not None:
            _LOGGER.debug("Updated %s: %s", self.name, state)
            self._position = state.position
            self._battery_level = state.battery_level
            self._rssi = state.rssi
            self._available = True
        else:
            self._available = False

    def _tilt(self, position):
        self._position = position
        self._bridge.set_blind_position(self._blind, position)
        self.schedule_update_ha_state()
