"""`ScourProtectionInstallation` and related processes."""

__author__ = "Rob Hammond"
__copyright__ = "Copyright 2020, National Renewable Energy Laboratory"
__maintainer__ = "Jake Nunemaker"
__email__ = "Jake.Nunemaker@nrel.gov"


from math import ceil

import simpy
from marmot import process

from ORBIT.core import Vessel
from ORBIT.core._defaults import process_times as pt
from ORBIT.phases.install import InstallPhase
from ORBIT.core.exceptions import InsufficientAmount, CargoWeightExceeded


class ScourProtectionInstallation(InstallPhase):
    """Scour protection installation simulation using a single vessel."""

    #:
    expected_config = {
        "spi_vessel": "dict | str",
        "site": {"distance": "int"},
        "plant": {
            "num_turbines": "int",
            "turbine_spacing": "int",
            "turbine_distance": "float (optional)",
        },
        "turbine": {"rotor_diameter": "int"},
        "port": {"monthly_rate": "float (optional)", "name": "str (optional)"},
        "scour_protection": {"tons_per_substructure": "int"},
    }

    phase = "Scour Protection Installation"

    def __init__(self, config, weather=None, **kwargs):
        """
        Creates an instance of `ScourProtectionInstallation`.

        Parameters
        ----------
        config : dict
            Simulation specific configuration.
        weather : np.ndarray
            Weather profile at site.
        """

        super().__init__(weather, **kwargs)

        config = self.initialize_library(config, **kwargs)
        self.config = self.validate_config(config)
        self.extract_defaults()

        self.setup_simulation(**kwargs)

    def setup_simulation(self, **kwargs):
        """
        Sets up the required simulation infrastructure:
            - creates a port
            - initializes a scour protection installation vessel
            - initializes vessel storage
        """

        self.initialize_port()
        self.initialize_spi_vessel()
        self.num_turbines = self.config["plant"]["num_turbines"]

        site_distance = self.config["site"]["distance"]
        rotor_diameter = self.config["turbine"]["rotor_diameter"]
        turbine_distance = self.config["plant"].get("turbine_distance", None)

        if turbine_distance is None:
            turbine_distance = (
                rotor_diameter
                * self.config["plant"]["turbine_spacing"]
                / 1000.0
            )

        self.tons_per_substructure = ceil(
            self.config["scour_protection"]["tons_per_substructure"]
        )

        install_scour_protection(
            self.spi_vessel,
            port=self.port,
            site_distance=site_distance,
            turbines=self.num_turbines,
            turbine_distance=turbine_distance,
            tons_per_substructure=self.tons_per_substructure,
            **kwargs,
        )

    def initialize_port(self):
        """
        Initializes a Port object with a simpy.Container of scour protection
        material.
        """

        self.port = simpy.Container(self.env)

    def initialize_spi_vessel(self):
        """
        Creates the scouring protection isntallation (SPI) vessel.
        """

        spi_specs = self.config["spi_vessel"]
        name = spi_specs.get("name", "SPI Vessel")

        spi_vessel = Vessel(name, spi_specs)
        self.env.register(spi_vessel)

        spi_vessel.extract_vessel_specs()
        spi_vessel.mobilize()
        spi_vessel.at_port = True
        spi_vessel.at_site = False
        self.spi_vessel = spi_vessel

    @property
    def detailed_output(self):
        """Returns detailed outputs."""

        outputs = {**self.agent_efficiencies}

        return outputs


@process
def install_scour_protection(
    vessel,
    port,
    site_distance,
    turbines,
    turbine_distance,
    tons_per_substructure,
    **kwargs,
):
    """
    Installs the scour protection. Processes the traveling between site
    and turbines for when there are enough rocks leftover from a previous
    installation as well as the weight of rocks available.

    Parameters
    ----------
    port : simpy.FilterStore
        Port simulation object.
    port_to_site_distance : int | float
        Distance (km) between site and the port.
    turbine_to_turbine_distance : int | float
        Distance between any two turbines.
        For now this assumes it traverses an edge and not a diagonal.
    turbines_to_install : int
        Number of turbines where scouring protection must be installed.
    tons_per_substructure : int
        Number of tons required to be installed at each substation
    """

    while turbines > 0:
        if vessel.at_port:
            # Load scour protection material
            yield load_material(
                vessel, vessel.rock_storage.available_capacity, **kwargs
            )

            # Transit to site
            vessel.at_port = False
            yield vessel.transit(site_distance)
            vessel.at_site = True

        elif vessel.at_site:
            if vessel.rock_storage.level >= tons_per_substructure:
                # Drop scour protection material
                yield drop_material(vessel, tons_per_substructure, **kwargs)
                turbines -= 1

                # Transit to another turbine
                if (
                    vessel.rock_storage.level >= tons_per_substructure
                    and turbines > 0
                ):
                    yield vessel.transit(turbine_distance)

                else:
                    # Transit back to port
                    vessel.at_site = False
                    yield vessel.transit(site_distance)
                    vessel.at_port = True

            else:
                # Transit back to port
                vessel.at_site = False
                yield vessel.transit(site_distance)
                vessel.at_port = True

        else:
            raise Exception("Vessel is lost at sea.")

    vessel.submit_debug_log(message="Scour Protection Installation Complete!")


@process
def load_material(vessel, weight, **kwargs):
    """
    A wrapper for simpy.Container.put that checks VesselStorageContainer
    constraints and triggers self.put() if successful.

    Items put into the instance should be a dictionary with the following
    attributes:
        - name
        - weight (t)
        - length (km)

    Parameters
    ----------
    item : dict
        Dictionary of item properties.
    """

    if vessel.rock_storage.level + weight > vessel.rock_storage.max_weight:
        raise CargoWeightExceeded(
            vessel.rock_storage.max_weight,
            vessel.rock_storage.level,
            "Scour Protection",
        )

    key = "load_rocks_time"
    load_time = kwargs.get(key, pt[key])

    vessel.rock_storage.put(weight)
    yield vessel.task(
        "Load SP Material",
        load_time,
        constraints=vessel.transit_limits,
        **kwargs,
    )


@process
def drop_material(vessel, weight, **kwargs):
    """
    Checks if there is enough of item, otherwise returns an error.

    Parameters
    ----------
    item_type : str
        Short, descriptive name of the item being accessed.
    item_amount : int or float
        Amount of the item to be loaded into storage.
    """

    if vessel.rock_storage.level < weight:
        raise InsufficientAmount(
            vessel.rock_storage.level, "Scour Protection", weight
        )

    key = "drop_rocks_time"
    drop_time = kwargs.get(key, pt[key])

    _ = vessel.rock_storage.get(weight)
    yield vessel.task(
        "Drop SP Material",
        drop_time,
        constraints=vessel.transit_limits,
        **kwargs,
    )
