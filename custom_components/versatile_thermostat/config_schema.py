""" All the schemas for ConfigFlow validation"""

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import selector
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.components.input_boolean import (
    DOMAIN as INPUT_BOOLEAN_DOMAIN,
)

from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.components.input_number import (
    DOMAIN as INPUT_NUMBER_DOMAIN,
)

from homeassistant.components.select import (
    DOMAIN as SELECT_DOMAIN,
)

from homeassistant.components.input_select import (
    DOMAIN as INPUT_SELECT_DOMAIN,
)

from homeassistant.components.input_datetime import (
    DOMAIN as INPUT_DATETIME_DOMAIN,
)

from homeassistant.components.person import DOMAIN as PERSON_DOMAIN
from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN


from .const import *  # pylint: disable=wildcard-import, unused-wildcard-import
from .heating_boiler_collector import (
    ACTIVE_COUNT_DOMAINS,
    BINARY_INPUT_DOMAINS,
    CENTRAL_MODE_DOMAINS,
    EXTERNAL_OWNER_DOMAINS,
    LEGACY_CYCLE_DOMAINS,
    MASTER_DOMAINS,
    ROOM_DOMAINS,
    SESSION_DOMAINS,
    SPLIT_DOMAINS,
)
from . import heating_pdc_collector as _pdc


def get_prop_function_options() -> list[str]:
    """Return proportional algorithm choices exposed in VT flows."""
    options = [PROPORTIONAL_FUNCTION_TPI]

    try:
        from .vtherm_central_api import VersatileThermostatAPI  # pylint: disable=import-outside-toplevel

        api = VersatileThermostatAPI.get_vtherm_api()
        if api is not None and hasattr(api, "list_prop_algorithms"):
            for name in api.list_prop_algorithms():
                if name not in options:
                    options.append(name)
    except Exception:  # pylint: disable=broad-except
        pass

    return options


def build_step_thermostat_switch_schema() -> vol.Schema:
    """Build the proportional switch thermostat schema with dynamic algorithms."""
    return vol.Schema(
        {
            vol.Required(CONF_UNDERLYING_LIST): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=[SWITCH_DOMAIN, INPUT_BOOLEAN_DOMAIN, SELECT_DOMAIN, INPUT_SELECT_DOMAIN, CLIMATE_DOMAIN], multiple=True),
            ),
            vol.Optional(CONF_HEATER_KEEP_ALIVE): cv.positive_int,
            vol.Required(CONF_PROP_FUNCTION, default=PROPORTIONAL_FUNCTION_TPI): vol.In(
                get_prop_function_options()
            ),
            vol.Optional(CONF_AC_MODE, default=False): cv.boolean,
            vol.Optional(CONF_INVERSE_SWITCH, default=False): cv.boolean,
            vol.Optional("on_command_text"): vol.In([]),
            vol.Optional(CONF_VSWITCH_ON_CMD_LIST): selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT, multiple=True)),
            vol.Optional("off_command_text"): vol.In([]),
            vol.Optional(CONF_VSWITCH_OFF_CMD_LIST): selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT, multiple=True)),
        }
    )


def build_step_thermostat_valve_schema() -> vol.Schema:
    """Build the valve thermostat schema with dynamic algorithms."""
    return vol.Schema(
        {
            vol.Required(CONF_UNDERLYING_LIST): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=[NUMBER_DOMAIN, INPUT_NUMBER_DOMAIN], multiple=True),
            ),
            vol.Required(CONF_PROP_FUNCTION, default=PROPORTIONAL_FUNCTION_TPI): vol.In(get_prop_function_options()),
            vol.Optional(CONF_AC_MODE, default=False): cv.boolean,
            vol.Optional(CONF_AUTO_REGULATION_DTEMP, default=10): vol.Coerce(float),
            vol.Optional(CONF_AUTO_REGULATION_PERIOD_MIN, default=5): cv.positive_int,
            vol.Optional(CONF_OPENING_THRESHOLD_DEGREE, default=0): vol.All(
                cv.positive_int, vol.Range(min=0, max=100)
            ),
            vol.Optional(CONF_MIN_OPENING_DEGREES, default=""): str,
            vol.Optional(CONF_MAX_OPENING_DEGREES, default=""): str,
            vol.Optional(CONF_MAX_CLOSING_DEGREE, default=100): vol.All(
                cv.positive_int, vol.Range(min=0, max=100)
            ),
        }
    )


def build_step_valve_regulation_schema() -> vol.Schema:
    """Build the valve regulation schema with dynamic algorithms."""
    return vol.Schema(
        {
            vol.Required(CONF_OPENING_DEGREE_LIST): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=[NUMBER_DOMAIN, INPUT_NUMBER_DOMAIN], multiple=True),
            ),
            vol.Optional(CONF_CLOSING_DEGREE_LIST): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=[NUMBER_DOMAIN, INPUT_NUMBER_DOMAIN], multiple=True),
            ),
            vol.Required(CONF_PROP_FUNCTION, default=PROPORTIONAL_FUNCTION_TPI): vol.In(
                get_prop_function_options()
            ),
            vol.Optional(CONF_OPENING_THRESHOLD_DEGREE, default=0): vol.All(
                cv.positive_int, vol.Range(min=0, max=100)
            ),
            vol.Optional(CONF_MIN_OPENING_DEGREES, default=""): str,
            vol.Optional(CONF_MAX_OPENING_DEGREES, default=""): str,
            vol.Optional(CONF_MAX_CLOSING_DEGREE, default=100): vol.All(
                cv.positive_int, vol.Range(min=0, max=100)
            ),
        }
    )

STEP_USER_DATA_SCHEMA = vol.Schema(  # pylint: disable=invalid-name
    {
        vol.Required(
            CONF_THERMOSTAT_TYPE, default=CONF_THERMOSTAT_SWITCH
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=CONF_THERMOSTAT_TYPES,
                translation_key="thermostat_type",
                mode="list",
            )
        )
    }
)

STEP_MAIN_DATA_SCHEMA = vol.Schema(  # pylint: disable=invalid-name
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_TEMP_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=[SENSOR_DOMAIN, INPUT_NUMBER_DOMAIN, NUMBER_DOMAIN]),
        ),
        vol.Optional(CONF_LAST_SEEN_TEMP_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=[SENSOR_DOMAIN, INPUT_DATETIME_DOMAIN, NUMBER_DOMAIN]),
        ),
        vol.Required(CONF_CYCLE_MIN, default=5): selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=1000, step=1, mode=selector.NumberSelectorMode.BOX)),

        vol.Optional(CONF_DEVICE_POWER, default="1"): vol.Coerce(float),
        vol.Optional(CONF_POWER_UNIT, default=POWER_UNIT_WATT): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=CONF_POWER_UNITS,
                translation_key="power_unit",
                mode="dropdown",
            )
        ),
        vol.Required(CONF_USE_MAIN_CENTRAL_CONFIG, default=True): cv.boolean,
        vol.Optional(CONF_USE_CENTRAL_MODE, default=True): cv.boolean,
        vol.Required(CONF_USED_BY_CENTRAL_BOILER, default=False): cv.boolean,
    }
)

STEP_FEATURES_DATA_SCHEMA = vol.Schema(  # pylint: disable=invalid-name
    {
        vol.Optional(CONF_USE_WINDOW_FEATURE, default=False): cv.boolean,
        vol.Optional(CONF_USE_MOTION_FEATURE, default=False): cv.boolean,
        vol.Optional(CONF_USE_POWER_FEATURE, default=False): cv.boolean,
        vol.Optional(CONF_USE_PRESENCE_FEATURE, default=False): cv.boolean,
        vol.Optional(CONF_USE_HEATING_FAILURE_DETECTION_FEATURE, default=False): cv.boolean,
    }
)

STEP_CLIMATE_FEATURES_DATA_SCHEMA = vol.Schema(  # pylint: disable=invalid-name
    {
        vol.Optional(CONF_USE_WINDOW_FEATURE, default=False): cv.boolean,
        vol.Optional(CONF_USE_MOTION_FEATURE, default=False): cv.boolean,
        vol.Optional(CONF_USE_POWER_FEATURE, default=False): cv.boolean,
        vol.Optional(CONF_USE_PRESENCE_FEATURE, default=False): cv.boolean,
        vol.Optional(CONF_USE_AUTO_START_STOP_FEATURE, default=False): cv.boolean,
    }
)

STEP_CLIMATE_VALVE_FEATURES_DATA_SCHEMA = vol.Schema(  # pylint: disable=invalid-name
    {
        vol.Optional(CONF_USE_WINDOW_FEATURE, default=False): cv.boolean,
        vol.Optional(CONF_USE_MOTION_FEATURE, default=False): cv.boolean,
        vol.Optional(CONF_USE_POWER_FEATURE, default=False): cv.boolean,
        vol.Optional(CONF_USE_PRESENCE_FEATURE, default=False): cv.boolean,
        vol.Optional(CONF_USE_HEATING_FAILURE_DETECTION_FEATURE, default=False): cv.boolean,
    }
)

STEP_CENTRAL_FEATURES_DATA_SCHEMA = vol.Schema(  # pylint: disable=invalid-name
    {
        vol.Optional(CONF_USE_WINDOW_FEATURE, default=False): cv.boolean,
        vol.Optional(CONF_USE_MOTION_FEATURE, default=False): cv.boolean,
        vol.Optional(CONF_USE_POWER_FEATURE, default=False): cv.boolean,
        vol.Optional(CONF_USE_PRESENCE_FEATURE, default=False): cv.boolean,
        vol.Optional(CONF_USE_CENTRAL_BOILER_FEATURE, default=False): cv.boolean,
        vol.Optional(CONF_USE_HEATING_FAILURE_DETECTION_FEATURE, default=False): cv.boolean,
    }
)

STEP_CENTRAL_MAIN_DATA_SCHEMA = vol.Schema(  # pylint: disable=invalid-name
    {
        vol.Required(CONF_EXTERNAL_TEMP_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=[SENSOR_DOMAIN, INPUT_NUMBER_DOMAIN]),
        ),
        vol.Required(CONF_TEMP_MIN, default=7): vol.Coerce(float),
        vol.Required(CONF_TEMP_MAX, default=35): vol.Coerce(float),

        vol.Required(CONF_STEP_TEMPERATURE, default=0.1): vol.Coerce(float),
    }
)

STEP_CENTRAL_SPEC_MAIN_DATA_SCHEMA = vol.Schema(  # pylint: disable=invalid-name
    {
        vol.Required(CONF_EXTERNAL_TEMP_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=[SENSOR_DOMAIN, INPUT_NUMBER_DOMAIN]),
        ),
        vol.Required(CONF_TEMP_MIN, default=7): vol.Coerce(float),
        vol.Required(CONF_TEMP_MAX, default=35): vol.Coerce(float),

        vol.Required(CONF_STEP_TEMPERATURE, default=0.1): vol.Coerce(float),
    }
)

STEP_CENTRAL_BOILER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CENTRAL_BOILER_ACTIVATION_DELAY_SEC, default=0): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=600, step=10, mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_CENTRAL_BOILER_ACTIVATION_SRV, default=""): str,
        vol.Optional(CONF_CENTRAL_BOILER_DEACTIVATION_SRV, default=""): str,
        vol.Optional(CONF_KEEP_ALIVE_BOILER_DELAY_SEC, default=DEFAULT_KEEP_ALIVE_BOILER_DELAY_SEC): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=3600, step=10, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="s")
        ),
    }
)

def _ho_entity_selector(domains, multiple: bool = False) -> selector.EntitySelector:
    """Entity selector restricted to the domains accepted by the orchestration collector."""
    return selector.EntitySelector(selector.EntitySelectorConfig(domain=list(domains), multiple=multiple))


def build_central_heating_orchestration_schema(allow_control: bool) -> vol.Schema:
    """Central entities of the boiler orchestration.

    ``control`` is offered only by the options flow of an existing central
    configuration (``allow_control``): it needs the confirmation step.
    """
    modes = HO_OPTIONS_MODES if allow_control else HO_SELECTABLE_MODES
    return vol.Schema(
        {
            vol.Required(CONF_HO_MODE, default=HO_MODE_DISABLED): selector.SelectSelector(
                selector.SelectSelectorConfig(options=list(modes), translation_key="heating_orchestration_mode", mode=selector.SelectSelectorMode.LIST)
            ),
            **_HO_CENTRAL_FIELDS,
            vol.Optional(CONF_HO_EXTERNAL_OWNERS): _ho_entity_selector(EXTERNAL_OWNER_DOMAINS, multiple=True),
        }
    )


# Boiler orchestration: the central entity fields shared by the creation and options flows.
_HO_CENTRAL_FIELDS = {
    vol.Optional(CONF_HO_MASTER): _ho_entity_selector(MASTER_DOMAINS),
    vol.Optional(CONF_HO_CENTRAL_MODE): _ho_entity_selector(CENTRAL_MODE_DOMAINS),
    vol.Optional(CONF_HO_ACTIVE_COUNT): _ho_entity_selector(ACTIVE_COUNT_DOMAINS),
    vol.Optional(CONF_HO_MANUAL): _ho_entity_selector(BINARY_INPUT_DOMAINS),
    vol.Optional(CONF_HO_PILOT_FILTER): _ho_entity_selector(BINARY_INPUT_DOMAINS),
    vol.Optional(CONF_HO_PILOT_READY): _ho_entity_selector(BINARY_INPUT_DOMAINS),
    vol.Optional(CONF_HO_CONSENTS): _ho_entity_selector(BINARY_INPUT_DOMAINS, multiple=True),
    vol.Optional(CONF_HO_LEGACY_CYCLE): _ho_entity_selector(LEGACY_CYCLE_DOMAINS),
}

# Creation flow of a new central configuration: observe at most.
STEP_CENTRAL_HEATING_ORCHESTRATION_SCHEMA = build_central_heating_orchestration_schema(False)
# Options flow of an existing central configuration: ``control`` selectable (with confirmation).
STEP_CENTRAL_HEATING_ORCHESTRATION_OPTIONS_SCHEMA = build_central_heating_orchestration_schema(True)

# Boiler orchestration (M2-B): explicit confirmation of the ``control`` mode.
STEP_HEATING_ORCHESTRATION_CONTROL_CONFIRM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HO_CONTROL_CONFIRM, default=False): selector.BooleanSelector(),
    }
)

# Boiler orchestration (M2-A): one room of the list (flat form, stored as a nested element).
STEP_HEATING_ORCHESTRATION_ROOM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HO_ROOM_KEY): selector.TextSelector(),
        vol.Required(CONF_HO_ROOM_CLIMATE): _ho_entity_selector(ROOM_DOMAINS),
        vol.Optional(CONF_HO_ROOM_PILOT_FILTER, default=False): selector.BooleanSelector(),
        vol.Optional(CONF_HO_PDC_SPLIT): _ho_entity_selector(SPLIT_DOMAINS),
        vol.Optional(CONF_HO_PDC_SESSION): _ho_entity_selector(SESSION_DOMAINS),
    }
)

# PDC (heat pump) runtime, observe only in this release: ``control`` is not offered.
# Global step: mode and the two entities shared by the PDC rooms.
def build_step_heating_pdc_schema(modes: list[str]) -> vol.Schema:
    """Mode and shared entities of the PDC orchestration.

    The caller decides which modes are offered: the release constant keeps
    ``disabled``/``observe``, while the options flow adds ``control`` only when
    the release gate allows it (the confirmation step follows).
    """
    return vol.Schema(
        {
            vol.Required(CONF_HO_PDC_MODE, default="disabled"): selector.SelectSelector(
                selector.SelectSelectorConfig(options=list(modes), translation_key="heating_pdc_mode", mode=selector.SelectSelectorMode.LIST)
            ),
            vol.Optional(_pdc.KEY_POWER): _ho_entity_selector(_pdc.SENSOR_DOMAINS),
            vol.Optional(_pdc.KEY_CENTRAL_MODE): _ho_entity_selector(_pdc.CENTRAL_MODE_DOMAINS),
        }
    )


STEP_HEATING_PDC_SCHEMA = build_step_heating_pdc_schema(HO_PDC_SELECTABLE_MODES)

# One PDC room (flat form, stored as a nested element of ``pdc_runtime.rooms``). The two rooms of the
# installation differ only by ``setpoint_min_c`` and ``manual_preset_bypass``.
STEP_HEATING_PDC_ROOM_SCHEMA = vol.Schema(
    {
        vol.Required(_pdc.KEY_ROOM_KEY): selector.TextSelector(),
        vol.Required(_pdc.KEY_RADIATOR): _ho_entity_selector(_pdc.CLIMATE_DOMAINS),
        vol.Required(_pdc.KEY_VALVE): _ho_entity_selector(_pdc.CLIMATE_DOMAINS),
        vol.Required(_pdc.KEY_TEMPERATURE): _ho_entity_selector(_pdc.SENSOR_DOMAINS),
        vol.Required(_pdc.KEY_WINDOW): _ho_entity_selector(_pdc.BINARY_DOMAINS),
        vol.Required(_pdc.KEY_SPLIT): _ho_entity_selector(_pdc.CLIMATE_DOMAINS),
        vol.Required(_pdc.KEY_PDC): _ho_entity_selector(_pdc.CLIMATE_DOMAINS),
        vol.Required(_pdc.KEY_AUTOMATIC): _ho_entity_selector(_pdc.BINARY_DOMAINS),
        vol.Required(_pdc.KEY_SCHEDULE_STOP): _ho_entity_selector(_pdc.SCHEDULE_DOMAINS),
        vol.Required(_pdc.KEY_MANAGED): _ho_entity_selector(_pdc.BINARY_DOMAINS),
        vol.Required(_pdc.KEY_OVERRIDE): _ho_entity_selector(_pdc.BINARY_DOMAINS),
        vol.Optional(_pdc.KEY_CYCLE): _ho_entity_selector(_pdc.TEXT_DOMAINS),
        vol.Optional(_pdc.KEY_OVERRIDE_TIMER): _ho_entity_selector(_pdc.TIMER_DOMAINS),
        vol.Optional(_pdc.KEY_SCHEDULE_SWITCHES): _ho_entity_selector(("switch",), multiple=True),
        vol.Optional(_pdc.KEY_EXTERNAL_OWNERS): _ho_entity_selector(_pdc.OWNER_DOMAINS, multiple=True),
        vol.Optional(_pdc.KEY_SETPOINT_MIN, default=16): selector.NumberSelector(selector.NumberSelectorConfig(min=5, max=30, step=1, mode=selector.NumberSelectorMode.BOX)),
        vol.Optional(_pdc.KEY_MANUAL_BYPASS, default=False): selector.BooleanSelector(),
    }
)

# PDC orchestration: explicit confirmation of the ``control`` mode (gated by the release flag).
STEP_HEATING_PDC_CONTROL_CONFIRM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HO_PDC_CONTROL_CONFIRM, default=False): selector.BooleanSelector(),
    }
)

STEP_THERMOSTAT_SWITCH = build_step_thermostat_switch_schema()  # pylint: disable=invalid-name

STEP_THERMOSTAT_CLIMATE = vol.Schema(  # pylint: disable=invalid-name
    {
        vol.Required(CONF_UNDERLYING_LIST): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=CLIMATE_DOMAIN, multiple=True),
        ),
        vol.Optional(CONF_AC_MODE, default=False): cv.boolean,
        vol.Optional(CONF_SYNC_DEVICE_INTERNAL_TEMP, default=False): cv.boolean,
        vol.Optional(CONF_AUTO_REGULATION_MODE, default=CONF_AUTO_REGULATION_NONE): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=CONF_AUTO_REGULATION_MODES,
                translation_key="auto_regulation_mode",
                mode="dropdown",
            )
        ),
        vol.Optional(CONF_AUTO_REGULATION_DTEMP, default=0.5): vol.Coerce(float),
        vol.Optional(CONF_AVATTO_ROOM_CONTROL, default=False): cv.boolean,
        vol.Optional(CONF_AUTO_REGULATION_PERIOD_MIN, default=5): cv.positive_int,
        vol.Optional(CONF_AUTO_FAN_MODE, default=CONF_AUTO_FAN_HIGH): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=CONF_AUTO_FAN_MODES,
                translation_key="auto_fan_mode",
                mode="dropdown",
            )
        ),
        vol.Optional(CONF_AUTO_REGULATION_USE_DEVICE_TEMP, default=False): cv.boolean,
    }
)

STEP_THERMOSTAT_VALVE = build_step_thermostat_valve_schema()  # pylint: disable=invalid-name

STEP_AUTO_START_STOP = vol.Schema(  # pylint: disable=invalid-name
    {
        vol.Optional(
            CONF_AUTO_START_STOP_LEVEL, default=AUTO_START_STOP_LEVEL_NONE
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=CONF_AUTO_START_STOP_LEVELS,
                translation_key="auto_start_stop",
                mode="dropdown",
            )
        ),
    }
)

STEP_VALVE_REGULATION = build_step_valve_regulation_schema()  # pylint: disable=invalid-name

STEP_SYNC_DEVICE_INTERNAL_TEMP = vol.Schema(  # pylint: disable=invalid-name
    {
        vol.Optional(CONF_SYNC_WITH_CALIBRATION, default=True): cv.boolean,
        vol.Required(CONF_SYNC_ENTITY_LIST): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=[NUMBER_DOMAIN, INPUT_NUMBER_DOMAIN], multiple=True),
        ),
    }
)

STEP_HUMIDITY_DATA_SCHEMA = vol.Schema(  # pylint: disable=invalid-name
    {
        vol.Required(CONF_USE_HUMIDITY_FEATURE, default=False): cv.boolean,
        vol.Optional(CONF_HUMIDITY_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=[SENSOR_DOMAIN, INPUT_NUMBER_DOMAIN, NUMBER_DOMAIN]),
        ),
    }
)

STEP_TPI_DATA_SCHEMA = vol.Schema(  # pylint: disable=invalid-name
    {
        vol.Required(CONF_USE_TPI_CENTRAL_CONFIG, default=True): cv.boolean,
    }
)

STEP_CENTRAL_TPI_DATA_SCHEMA = vol.Schema(  # pylint: disable=invalid-name
    {
        vol.Required(CONF_TPI_COEF_INT, default=0.6): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0.0, max=10.0, step="any", mode=selector.NumberSelectorMode.BOX
            )
        ),
        vol.Required(CONF_TPI_COEF_EXT, default=0.01): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0.0, max=1.0, step="any", mode=selector.NumberSelectorMode.BOX
            )
        ),
        vol.Required(CONF_MINIMAL_ACTIVATION_DELAY, default=10): cv.positive_int,
        vol.Required(CONF_MINIMAL_DEACTIVATION_DELAY, default=0): cv.positive_int,
        vol.Optional(CONF_TPI_THRESHOLD_LOW, default=0): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=-10.0, max=10.0, step=0.1, mode=selector.NumberSelectorMode.BOX
            )
        ),
        vol.Optional(CONF_TPI_THRESHOLD_HIGH, default=0): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=-10.0, max=10.0, step=0.1, mode=selector.NumberSelectorMode.BOX
            )
        ),
        vol.Optional(CONF_AUTO_TPI_MODE, default=False): cv.boolean,
    }
)
# Duplicating schema for central config
# as we don't want to display the use_auto_tpi checkbox
# in central config but only in device config.
STEP_CENTRAL_TPI_DATA_SCHEMA_CENTRAL = vol.Schema(  # pylint: disable=invalid-name
    {
        vol.Required(CONF_TPI_COEF_INT, default=0.6): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0.0, max=10.0, step=0.01, mode=selector.NumberSelectorMode.BOX
            )
        ),
        vol.Required(CONF_TPI_COEF_EXT, default=0.01): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0.0, max=1.0, step=0.001, mode=selector.NumberSelectorMode.BOX
            )
        ),
        vol.Required(CONF_MINIMAL_ACTIVATION_DELAY, default=10): cv.positive_int,
        vol.Required(CONF_MINIMAL_DEACTIVATION_DELAY, default=0): cv.positive_int,
        vol.Optional(CONF_TPI_THRESHOLD_LOW, default=0): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=-10.0, max=10.0, step=0.1, mode=selector.NumberSelectorMode.BOX
            )
        ),
        vol.Optional(CONF_TPI_THRESHOLD_HIGH, default=0): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=-10.0, max=10.0, step=0.1, mode=selector.NumberSelectorMode.BOX
            )
        ),
    }
)

STEP_PRESETS_DATA_SCHEMA = vol.Schema(  # pylint: disable=invalid-name
    {
        vol.Required(CONF_USE_PRESETS_CENTRAL_CONFIG, default=True): cv.boolean,
    }
)

STEP_WINDOW_DATA_SCHEMA = vol.Schema(  # pylint: disable=invalid-name
    {
        vol.Optional(CONF_WINDOW_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=[BINARY_SENSOR_DOMAIN, INPUT_BOOLEAN_DOMAIN]
            ),
        ),
        vol.Required(CONF_USE_WINDOW_CENTRAL_CONFIG, default=True): cv.boolean,
    }
)

STEP_CENTRAL_WINDOW_DATA_SCHEMA = vol.Schema(  # pylint: disable=invalid-name
    {
        vol.Optional(CONF_WINDOW_DELAY, default=30): cv.positive_int,
        vol.Optional(CONF_WINDOW_OFF_DELAY, default=30): cv.positive_int,
        vol.Optional(CONF_WINDOW_AUTO_OPEN_THRESHOLD, default=3): vol.Coerce(float),
        vol.Optional(CONF_WINDOW_AUTO_CLOSE_THRESHOLD, default=0): vol.Coerce(float),
        vol.Optional(CONF_WINDOW_AUTO_MAX_DURATION, default=30): cv.positive_int,
        vol.Optional(CONF_WINDOW_ACTION, default=CONF_WINDOW_TURN_OFF): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=CONF_WINDOW_ACTIONS,
                translation_key="window_action",
                mode="dropdown",
            )
        ),
    }
)

STEP_CENTRAL_WINDOW_WO_AUTO_DATA_SCHEMA = vol.Schema(  # pylint: disable=invalid-name
    {
        vol.Optional(CONF_WINDOW_DELAY, default=30): cv.positive_int,
        vol.Optional(CONF_WINDOW_OFF_DELAY, default=30): cv.positive_int,
        vol.Optional(CONF_WINDOW_ACTION, default=CONF_WINDOW_TURN_OFF): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=CONF_WINDOW_ACTIONS,
                translation_key="window_action",
                mode="dropdown",
            )
        ),
    }
)

STEP_MOTION_DATA_SCHEMA = vol.Schema(  # pylint: disable=invalid-name
    {
        vol.Required(CONF_MOTION_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=[BINARY_SENSOR_DOMAIN, INPUT_BOOLEAN_DOMAIN]
            ),
        ),
        vol.Required(CONF_USE_MOTION_CENTRAL_CONFIG, default=True): cv.boolean,
    }
)

STEP_CENTRAL_MOTION_DATA_SCHEMA = vol.Schema(  # pylint: disable=invalid-name
    {
        vol.Optional(CONF_MOTION_DELAY, default=30): cv.positive_int,
        vol.Optional(CONF_MOTION_OFF_DELAY, default=300): cv.positive_int,
        vol.Optional(CONF_MOTION_PRESET, default="comfort"): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=CONF_PRESETS_SELECTIONABLE,
                translation_key="presets",
                mode="dropdown",
            )
        ),
        vol.Optional(CONF_NO_MOTION_PRESET, default="eco"): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=CONF_PRESETS_SELECTIONABLE,
                translation_key="presets",
                mode="dropdown",
            )
        ),
    }
)

STEP_CENTRAL_POWER_DATA_SCHEMA = vol.Schema(  # pylint: disable=invalid-name
    {
        vol.Required(CONF_POWER_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=[SENSOR_DOMAIN, INPUT_NUMBER_DOMAIN]),
        ),
        vol.Required(CONF_MAX_POWER_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=[SENSOR_DOMAIN, INPUT_NUMBER_DOMAIN]),
        ),
        vol.Optional(CONF_POWER_UNIT, default=POWER_UNIT_AUTO): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=CONF_CENTRAL_POWER_UNITS,
                translation_key="central_power_unit",
                mode="dropdown",
            )
        ),
        vol.Optional(CONF_PRESET_POWER, default="13"): vol.Coerce(float),
    }
)

STEP_NON_CENTRAL_POWER_DATA_SCHEMA = vol.Schema(  # pylint: disable=invalid-name
    {
        vol.Optional(CONF_PRESET_POWER, default="13"): vol.Coerce(float),
    }
)

STEP_POWER_DATA_SCHEMA = vol.Schema(  # pylint: disable=invalid-name
    {
        vol.Required(CONF_USE_POWER_CENTRAL_CONFIG, default=True): cv.boolean,
    }
)

STEP_CENTRAL_PRESENCE_DATA_SCHEMA = vol.Schema(  # pylint: disable=invalid-name
    {
        vol.Required(CONF_PRESENCE_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=[
                    PERSON_DOMAIN,
                    BINARY_SENSOR_DOMAIN,
                    INPUT_BOOLEAN_DOMAIN,
                ]
            ),
        )
    },
)

STEP_PRESENCE_DATA_SCHEMA = vol.Schema(  # pylint: disable=invalid-name
    {
        vol.Required(CONF_USE_PRESENCE_CENTRAL_CONFIG, default=True): cv.boolean,
    }
)

STEP_CENTRAL_ADVANCED_DATA_SCHEMA = vol.Schema(  # pylint: disable=invalid-name
    {
        vol.Required(CONF_SAFETY_DELAY_MIN, default=60): cv.positive_int,
        vol.Required(
            CONF_SAFETY_MIN_ON_PERCENT,
            default=DEFAULT_SAFETY_MIN_ON_PERCENT,
        ): vol.Coerce(float),
        vol.Required(
            CONF_SAFETY_DEFAULT_ON_PERCENT,
            default=DEFAULT_SAFETY_DEFAULT_ON_PERCENT,
        ): vol.Coerce(float),
        vol.Optional(CONF_REPAIR_INCORRECT_STATE, default=False): cv.boolean,
        vol.Optional(CONF_MANUAL_OVERRIDE_DURATION_MIN, default=0): cv.positive_int,
    }
)

STEP_ADVANCED_DATA_SCHEMA = vol.Schema(  # pylint: disable=invalid-name
    {
        vol.Required(CONF_USE_ADVANCED_CENTRAL_CONFIG, default=True): cv.boolean,
    }
)

STEP_LOCK_DATA_SCHEMA = vol.Schema(  # pylint: disable=invalid-name
    {
        vol.Required(CONF_USE_LOCK_CENTRAL_CONFIG, default=True): cv.boolean,
    }
)

STEP_CENTRAL_LOCK_DATA_SCHEMA = vol.Schema(  # pylint: disable=invalid-name
    {
        vol.Optional(CONF_LOCK_CODE): cv.string,
        vol.Optional(CONF_LOCK_USERS, default=True): cv.boolean,
        vol.Optional(CONF_LOCK_AUTOMATIONS, default=True): cv.boolean,
        vol.Optional(CONF_AUTO_RELOCK_SEC, default=30): vol.Coerce(int),
    }
)

STEP_CENTRAL_HEATING_FAILURE_DETECTION_SCHEMA = vol.Schema(  # pylint: disable=invalid-name
    {
        vol.Required(
            CONF_HEATING_FAILURE_THRESHOLD,
            default=DEFAULT_HEATING_FAILURE_THRESHOLD,
        ): selector.NumberSelector(selector.NumberSelectorConfig(min=0.0, max=1.0, step=0.01, mode=selector.NumberSelectorMode.BOX)),
        vol.Required(
            CONF_COOLING_FAILURE_THRESHOLD,
            default=DEFAULT_COOLING_FAILURE_THRESHOLD,
        ): selector.NumberSelector(selector.NumberSelectorConfig(min=0.0, max=1.0, step=0.01, mode=selector.NumberSelectorMode.BOX)),
        vol.Required(
            CONF_HEATING_FAILURE_DETECTION_DELAY,
            default=DEFAULT_HEATING_FAILURE_DETECTION_DELAY,
        ): cv.positive_int,
        vol.Required(
            CONF_TEMPERATURE_CHANGE_TOLERANCE,
            default=DEFAULT_TEMPERATURE_CHANGE_TOLERANCE,
        ): selector.NumberSelector(selector.NumberSelectorConfig(min=0.0, max=5.0, step=0.1, mode=selector.NumberSelectorMode.BOX)),
        vol.Optional(
            CONF_FAILURE_DETECTION_ENABLE_TEMPLATE,
        ): selector.TemplateSelector(),
    }
)

STEP_HEATING_FAILURE_DETECTION_SCHEMA = vol.Schema(  # pylint: disable=invalid-name
    {
        vol.Required(CONF_USE_HEATING_FAILURE_DETECTION_CENTRAL_CONFIG, default=True): cv.boolean,
    }
)

STEP_AUTO_TPI_CONFIGURATION_SCHEMA = vol.Schema(
    {
        vol.Required(
            CONF_AUTO_TPI_LEARNING_TYPE, default=AUTO_TPI_LEARNING_TYPE_DISCOVERY
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=CONF_AUTO_TPI_LEARNING_TYPES,
                translation_key="auto_tpi_learning_type",
                mode="dropdown",
            )
        ),
        vol.Required(CONF_AUTO_TPI_AGGRESSIVENESS, default=1.0): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0.5, max=1.0, step=0.01, mode=selector.NumberSelectorMode.SLIDER
            )
        ),
        vol.Optional(CONF_AUTO_TPI_HEATER_HEATING_TIME, default=5): cv.positive_int,
        vol.Optional(CONF_AUTO_TPI_HEATER_COOLING_TIME, default=5): cv.positive_int,
        vol.Required(CONF_AUTO_TPI_HEATING_POWER, default=0.0): vol.Coerce(float),
        vol.Optional(CONF_AUTO_TPI_CONTINUOUS_KEXT, default=False): cv.boolean,
        vol.Optional(
            CONF_AUTO_TPI_ENABLE_ADVANCED_SETTINGS, default=False
        ): cv.boolean,
    }
)


STEP_AUTO_TPI_AVG_SETTINGS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_AUTO_TPI_AVG_INITIAL_WEIGHT, default=1): cv.positive_int,
    }
)

STEP_AUTO_TPI_EMA_SETTINGS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_AUTO_TPI_EMA_ALPHA, default=0.15): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0.0, max=1.0, step=0.01, mode=selector.NumberSelectorMode.BOX
            )
        ),
        vol.Required(
            CONF_AUTO_TPI_EMA_DECAY_RATE, default=0.08
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0.0, max=1.0, step=0.01, mode=selector.NumberSelectorMode.BOX
            )
        ),
        vol.Required(CONF_AUTO_TPI_CONTINUOUS_KEXT_ALPHA, default=0.04): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0.005, max=0.2, step=0.005, mode=selector.NumberSelectorMode.BOX
            )
        ),
    }
)
