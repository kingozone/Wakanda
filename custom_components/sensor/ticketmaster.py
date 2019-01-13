"""
Custom component to interface with ticketmaster
"""
import logging
from datetime import datetime
from urllib.parse import urlencode

import pytz
import voluptuous as vol

from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.const import CONF_API_KEY, CONF_NAME
from homeassistant.helpers.entity import Entity
import homeassistant.helpers.config_validation as cv

CONF_CITY = 'city'
CONF_COUNTRY = 'country'
CONF_SORT = 'sort'
CONF_KEYWORD = 'keyword'

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_API_KEY): cv.string,
    vol.Optional(CONF_NAME): cv.string,
    vol.Optional(CONF_CITY): cv.string,
    vol.Optional(CONF_COUNTRY): cv.string,
    vol.Optional(CONF_SORT): cv.string,
    vol.Optional(CONF_KEYWORD): cv.string,
})

_LOGGER = logging.getLogger(__name__)


class Ticketmaster:

    URL = "https://app.ticketmaster.com//discovery/v2/events.json?%s"

    @staticmethod
    async def fetch(session, url):
        import json
        async with session.get(url) as response:
            return json.loads(await response.text())['_embedded']['events'][0]

    @staticmethod
    async def get_data(url):
        import aiohttp
        async with aiohttp.ClientSession() as session:
            html = await Ticketmaster.fetch(session, url)
            return html


async def async_setup_platform(
        hass, config, async_add_entities, discovery_info=None):
    """Set up the asuswrt sensors."""
    params = (
        ('apikey', config[CONF_API_KEY]),
        ('includeTest', 'no'),
        ('onsaleStartDateTime', datetime.utcnow().strftime("%Y-%m-%dT%TZ")),
        ('size', 1)
    )
    if config.get(CONF_SORT):
        params += ((CONF_SORT, config[CONF_SORT]),)
    if config.get(CONF_CITY):
        params += ((CONF_CITY, config[CONF_CITY]),)
    if config.get(CONF_COUNTRY):
        params += (('countryCode', config[CONF_COUNTRY]),)
    if config.get(CONF_KEYWORD):
        params += ((CONF_KEYWORD, config[CONF_KEYWORD]),)
    data = Ticketmaster()
    url = Ticketmaster.URL % urlencode(params)
    data = await data.get_data(url)
    entities = [
        TicketmasterSensor(data, url, config.get('name', 'Ticketmaster')),
    ]
    async_add_entities(entities)


class TicketmasterSensor(Entity):

    def __init__(self, data, url, name):
        self._unit = 'event'
        self._name = name
        self._state = data.get('name')
        self._url = url
        self._attributes = {}
        self.update_attributes(data)

    @property
    def unit_of_measurement(self):
        return self._unit

    @property
    def name(self):
        return self._name

    @property
    def state(self):
        return self._state

    @property
    def state_attributes(self):
        return self._attributes

    def update_attributes(self, data):
        if data:
            self._attributes['url'] = data['url']
            self._attributes[
                'Sales start time'] = data['sales']['public']['startDateTime']
            self._attributes[
                'Sales stop time'] = data['sales']['public']['endDateTime']
            self._attributes[
                'Event start time'] = "%s %s" % (
                data['dates']['start']['localDate'],
                data['dates']['start']['localTime']
            )
            self._attributes['city'] = data[
                '_embedded']['venues'][0]['city']['name']

    async def async_update(self):
        data = await Ticketmaster.get_data(self._url)
        self._state = data.get('name')
        self.update_attributes(data)
