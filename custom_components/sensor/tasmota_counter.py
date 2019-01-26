"""
Support Tasmota MQTT counter sensor

Tasmota forget the counter value at reset.
It is a problem to save it to Flash any update (wear the flash)
The solution is to check uptime and add diff to the counter value 

"""
import logging
import json
import re
from datetime import timedelta
from typing import Optional

import voluptuous as vol

from homeassistant.core import callback
from homeassistant.components import sensor
from homeassistant.components.mqtt import (
    CONF_AVAILABILITY_TOPIC, CONF_STATE_TOPIC,
    CONF_QOS,
    MqttAvailability)
from homeassistant.components.sensor import DEVICE_CLASSES_SCHEMA
from homeassistant.const import (
     CONF_NAME, CONF_VALUE_TEMPLATE, STATE_UNKNOWN,
    CONF_UNIT_OF_MEASUREMENT, CONF_ICON, CONF_DEVICE_CLASS)
from homeassistant.helpers.entity import Entity
from homeassistant.components import mqtt
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import HomeAssistantType, ConfigType
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import (async_track_point_in_utc_time,async_track_time_interval)
from homeassistant.util import dt as dt_util
from homeassistant.helpers.restore_state import async_get_last_state

_LOGGER = logging.getLogger(__name__)


DOMAIN = 'tasmota_counter'

DEPENDENCIES = ['mqtt']
CONF_SHORT_TOPIC ='stopic' # short_topic
CONF_COUNTER_ID = 'counter_id'
CONF_MAX_DIFF = 'max_valid_diff'
CONF_EXPIRE_AFTER = 'expire_after'


def get_tasmota_avail_topic (topic):
    return ('tele/{}/LWT'.format(topic))

def get_tasmota_tele (topic):
    return ('tele/{}/SENSOR'.format(topic))

def get_tasmota_state (topic):
     return ('tele/{}/STATE'.format(topic))


PLATFORM_SCHEMA = cv.PLATFORM_SCHEMA.extend({
    vol.Required(CONF_NAME): cv.string,
    vol.Required(CONF_SHORT_TOPIC): cv.string,
    vol.Required(CONF_COUNTER_ID):cv.positive_int,
    vol.Required(CONF_MAX_DIFF):cv.positive_int,
    vol.Optional(CONF_VALUE_TEMPLATE): cv.template,
    vol.Optional(CONF_UNIT_OF_MEASUREMENT): cv.string,
    vol.Optional(CONF_ICON): cv.icon,
    vol.Optional(CONF_EXPIRE_AFTER): cv.positive_int,
})


async def async_setup_platform(hass: HomeAssistantType, config: ConfigType,
                               async_add_entities, discovery_info=None):
    """Set up MQTT sensors through configuration.yaml."""
    await _async_setup_entity(hass, config, async_add_entities)



async def _async_setup_entity(hass: HomeAssistantType, config: ConfigType,
                              async_add_entities, discovery_hash=None):
    """Set up MQTT sensor."""

    async_add_entities([MqttTasmotaCounter(
        hass,
        config.get(CONF_NAME),
        config.get(CONF_SHORT_TOPIC),
        config.get(CONF_UNIT_OF_MEASUREMENT),
        config.get(CONF_COUNTER_ID),
        config.get(CONF_EXPIRE_AFTER),
        config.get(CONF_ICON),
        config.get(CONF_VALUE_TEMPLATE),
        config.get(CONF_MAX_DIFF)

    )])
######
#
#09:16:59 MQT: tele/water_out/STATE = {"Time":"2018-12-21T09:16:59","Uptime":"0T00:00:23","Vcc":2.723,"Wifi":{"AP":1,"SSId":"fbi-4","RSSI":36,"APMac":"F8:D1:11:A0:AA:68"}}
#09:16:59 MQT: tele/water_out/SENSOR = {"Time":"2018-12-21T09:16:59","COUNTER":{"C1":0}}
#
######


ATTR_VALUE = 'value'
ATTR_OLD_VALUE = 'old_value'
ATTR_UPTIME = 'uptime_sec'
DEFAULT_QOS = 1


class MqttTasmotaCounter(MqttAvailability,  Entity):
    """Representation of a Tasmota counter using MQTT."""

    def __init__(self, hass, name, topic,  
                 unit_of_measurement,
                 counter_id,
                 expire_after,
                 icon, value_template,
                 max_valid_diff):
        """Initialize the sensor."""
        MqttAvailability.__init__(self, get_tasmota_avail_topic(topic), DEFAULT_QOS,
                                  "Online", "Offline")
        self.hass = hass
        self._state = STATE_UNKNOWN
        self._old_value = None
        self._value = None # counter value 
        self._uptime_sec = 100000000000 # uptime in seconds
        self._valid_ref = False
        self._name = name
        self._max_valid_diff = max_valid_diff
        self._state_tele = get_tasmota_tele (topic)
        self._state_state = get_tasmota_state (topic)
        self._expire_after = expire_after
        self._couner_id = counter_id
        self._qos = DEFAULT_QOS
        self._unit_of_measurement = unit_of_measurement
        self._template = value_template
        self._mqtt_update = True
        self._icon = icon

        # start keepalive 
        if self._expire_after is not None and self._expire_after > 0:
            async_track_time_interval(
                 self.hass, self._async_keepalive, timedelta(seconds=self._expire_after))


    async def async_added_to_hass(self):
        """Subscribe mqtt events."""
        await self.load_state_from_recorder() # load from the value from recorder

        await MqttAvailability.async_added_to_hass(self)

        @callback
        def tele_received(topic, payload, qos):
            self.update_mqtt_sensor(payload)

        @callback
        def state_received(topic, payload, qos):
            self.update_mqtt_state(payload)

        await mqtt.async_subscribe(
            self.hass, self._state_state, state_received, self._qos)
        await mqtt.async_subscribe(
            self.hass, self._state_tele, tele_received, self._qos)

    #COUNTER":{"C1":0}
    def update_mqtt_sensor (self,payload):
        CNT='COUNTER'
        try:
            message = json.loads(payload)
            if CNT in message:
                c='C{}'.format(self._couner_id)
                new_counter=int(message[CNT][c])
                self.update_counter(new_counter)
            else:
               _LOGGER.error("Unable to find %s in payload %s", CNT,payload)
               return
        except ValueError:
            # If invalid JSON
          _LOGGER.error("Unable to parse payload as JSON: %s", payload)
          return


    async def _async_keepalive(self,time=None):
        # kind of watchdog 
        if self._mqtt_update:
            self._mqtt_update = False
        else:
            self._m_state = STATE_UNKNOWN
            self.async_schedule_update_ha_state()


    def update_counter(self,new_counter):
        if self._valid_ref:
            diff = 0  
            if new_counter < self._old_value:
                # check wrap of 64bit tasmota save it as 64bit 
               diff = (new_counter + 0xffffffff + 1) - self._old_value 
               if diff > self._max_valid_diff :
                  new_counter = self._old_value
                  _LOGGER.error("New counter (%d) is smaller than old (%d) -- we missed uptime?",new_counter ,self._old_value)
                  return;
            else:    
              diff = new_counter - self._old_value

            if diff == 0:
                return;
            if diff > self._max_valid_diff:
                _LOGGER.error("diff is too high (%d) somthing is wrong new:(%d) - old:(%d) ", diff,new_counter ,self._old_value)
            else:
               self._mqtt_update = True
               self._value += diff
               self._old_value = new_counter
               self.update_state_value ()
               self.async_schedule_update_ha_state()
        else:
            # set new value 
            self._old_value = new_counter
            self._valid_ref = True


    def update_state_value (self):
        if self._template is None:
           self._state = self._value
        else:
           self._template.hass = self.hass
           self._state = self._template.async_render({'value': self._value})


    def uptime_to_sec (self,uptime_str):
        """ convert uptime string to sec """
        m = re.match(r"(\d+)T(\d\d)\:(\d\d)\:(\d\d)", uptime_str)
        if m:
            return(int(m.group(4))+(int(m.group(3))*60)+int(m.group(2))*60*60+int(m.group(1))*60*60*24)
        _LOGGER.error("Unable to parse uptime string %s", uptime_str)
        return (-1)


    #"Uptime":"0T00:00:23"
    def update_mqtt_state (self,payload):
        UT='Uptime'
        try:
            message = json.loads(payload)
            if UT in message:
                uptime_str=message[UT]
                uptime_sec = self.uptime_to_sec(uptime_str)
                self.update_uptime(uptime_sec)
            else:
              _LOGGER.error("Unable to find %s  in payload %s", UT,payload)
              return

        except ValueError:
            # If invalid JSON
          _LOGGER.error("Unable to parse payload as JSON: %s", payload)
          return


    def update_uptime(self,uptime_sec):
        if uptime_sec < 0:
            return # nothing to update
        if uptime_sec < self._uptime_sec:
           # we had a reboot, need to take a new ref
           self._valid_ref = False 
           self._uptime_sec = uptime_sec
        else:
            self._uptime_sec = uptime_sec
        

    async def load_state_from_recorder (self):
        if self._value is not None:
           return

        state = await async_get_last_state(self.hass, self.entity_id)
        if not state:
            _LOGGER.info(" async_added_to_hass can't find stats %s",str(state)) 
            # first time init
            self._value = 0
            self._old_value = 0
            self._valid_ref = False
            self.update_state_value (self)
            self.async_schedule_update_ha_state()
            return

        self._state = state.state

        _LOGGER.info("async_added_to_hass %s",str(state.attributes)) 

        # restore from recorder
        for attr, var, t in ((ATTR_VALUE,'_value',int),
                             (ATTR_OLD_VALUE,'_old_value',int),
                             (ATTR_UPTIME,'_uptime_sec',int)
                     ):
            if attr in state.attributes:
                try:
                  setattr(self, var, t(state.attributes[attr]))
                except Exception as e:
                  _LOGGER.error("converting %s to %s ",state.attributes[attr],var)

        if self._value is None:
            self._value =0

       # update valid ref 
        if self._uptime_sec > 0:
           self._valid_ref = True
        self.update_state_value ()


    @property
    def should_poll(self):
        """No polling needed."""
        return False

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def unit_of_measurement(self):
        """Return the unit this state is expressed in."""
        return self._unit_of_measurement

    @property
    def state(self):
        """Return the state of the entity."""
        return self._state 

    @property
    def device_state_attributes(self):
        """Return the state attributes."""
        if self._value is None:
            return {}

        return {
            ATTR_VALUE: self._value,
            ATTR_OLD_VALUE: self._old_value,
            ATTR_UPTIME : self._uptime_sec
        }

    @property
    def icon(self):
        """Return the icon."""
        return self._icon


