"""Exercise the Avatto policy through the normal climate controls."""

import pytest

from custom_components.versatile_thermostat.avatto import heating_setpoint, room_demand

from .commons import (
    CONF_AVATTO_ROOM_CONTROL,
    DOMAIN,
    MockConfigEntry,
    PARTIAL_CLIMATE_NOT_REGULATED_CONFIG,
    VThermHvacMode_HEAT,
    VThermHvacMode_OFF,
    create_thermostat,
    send_temperature_change_event,
    wait_for_local_condition,
)
from .test_setpoint_modifier import record_commands


@pytest.mark.parametrize(
    ("current", "was_heating", "expected"),
    [(17.8, True, False), (17.5, True, False), (17.4, True, True), (17.4, False, False), (17.2, False, True), (None, False, None)],
)
def test_room_demand(current, was_heating, expected):
    """Demand has different thresholds for starting and stopping."""
    assert room_demand(current, 17.5, was_heating) is expected


def test_boost_limit_survives_quantization():
    """A setpoint on the regulation grid must stay within the boost limit."""
    assert heating_setpoint(19, 17.8, 0.5) == 18
    assert heating_setpoint(17.5, 17.8, 0.5) == 17.5


async def test_avatto_handoff_and_temperature_changes(hass, skip_send_event, fake_temp_sensor, fake_ext_temp_sensor, fake_underlying_climate):
    """Send standby before re-enabling the valve and react at hysteresis boundaries."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Avatto test",
        unique_id="avatto_test",
        data=PARTIAL_CLIMATE_NOT_REGULATED_CONFIG | {CONF_AVATTO_ROOM_CONTROL: True},
    )
    entity = await create_thermostat(hass, entry, "climate.avatto_test")
    await wait_for_local_condition(lambda: entity.is_ready)
    now = entity.now
    await entity.async_set_hvac_mode(VThermHvacMode_OFF)
    await entity.async_set_temperature(temperature=17.5)
    await send_temperature_change_event(entity, 17.8, now)
    commands = await record_commands(entity, VThermHvacMode_HEAT)
    valve = entity._underlyings[0]
    standby = valve.min_temp if valve.min_temp is not None else entity.min_temp
    assert commands[0] == ("temperature", standby)
    assert commands[1][0] == "hvac_mode"

    await send_temperature_change_event(entity, 17.4, now)
    assert valve.last_sent_temperature == standby
    await send_temperature_change_event(entity, 17.2, now)
    assert valve.last_sent_temperature == 17.5
    await send_temperature_change_event(entity, 17.4, now)
    assert valve.last_sent_temperature == 17.5
    await send_temperature_change_event(entity, 17.5, now)
    assert valve.last_sent_temperature == standby
    entity.remove_thermostat()
