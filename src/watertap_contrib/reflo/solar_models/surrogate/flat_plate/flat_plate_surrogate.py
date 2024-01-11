###############################################################################
# WaterTAP Copyright (c) 2021, The Regents of the University of California,
# through Lawrence Berkeley National Laboratory, Oak Ridge National
# Laboratory, National Renewable Energy Laboratory, and National Energy
# Technology Laboratory (subject to receipt of any required approvals from
# the U.S. Dept. of Energy). All rights reserved.
#
# Please see the files COPYRIGHT.md and LICENSE.md for full copyright and license
# information, respectively. These files are also available online at the URL
# "https://github.com/watertap-org/watertap/"
#
###############################################################################

import os

from pyomo.environ import (
    Var,
    Param,
    Constraint,
    units as pyunits,
    check_optimal_termination,
)

from idaes.core import declare_process_block_class
import idaes.core.util.scaling as iscale
from idaes.core.solvers.get_solver import get_solver
from idaes.core.util.exceptions import InitializationError
import idaes.logger as idaeslog

from watertap_contrib.seto.core import SolarEnergyBaseData

__author__ = "Matthew Boyd, Kurban Sitterley"


@declare_process_block_class("FlatPlateSurrogate")
class FlatPlateSurrogateData(SolarEnergyBaseData):
    """
    Surrogate model for flat plate.
    """

    def build(self):
        super().build()

        self._tech_type = "flat_plate"

        self.specific_heat_water = Param(
            initialize=4.181,  # defaults from SAM
            units=pyunits.kJ / (pyunits.kg * pyunits.K),
            doc="Specific heat of water",
        )
        self.dens_water = Param(
            initialize=1000,  # defaults from SAM
            units=pyunits.kg / pyunits.m**3,
            mutable=True,
            doc="Density of water",
        )

        self.temperature_cold = Param(
            initialize=293,  # defaults from SAM
            units=pyunits.K,
            mutable=True,
            doc="Cold temperature",
        )

        self.factor_delta_T = Param(
            initialize=0.03,  # this is a guess as to what the 30 represents in the equation for total collector area in SAM documentation
            units=pyunits.K,
            mutable=True,
            doc="Influent minus ambient temperature",
        )

        self.collector_area_per = Param(
            initialize=2.98,  # defaults from SAM
            units=pyunits.m**2,
            mutable=True,
            doc="Area for single collector",
        )

        self.FR_ta = Param(
            initialize=0.689,  # optical gain "a" in Hottel-Whillier-Bliss equation [hcoll = a - b*dT]; defaults from SAM
            units=pyunits.kilowatt / pyunits.m**2,
            mutable=True,
            doc="Product of collector heat removal factor (FR), cover transmittance (t), and shortwave absorptivity of absorber (a)",
        )

        self.FR_UL = Param(
            initialize=3.85,  # Thermal loss coeff "b" in Hottel-Whillier-Bliss equation [hcoll = a - b*dT]; defaults from SAM
            units=pyunits.kilowatt / (pyunits.m**2 * pyunits.K),
            mutable=True,
            doc="Product of collector heat removal factor (FR) and overall heat loss coeff. of collector (UL)",
        )

        self.collector_area_total = Var(
            initialize=3,
            units=pyunits.m**2,
            bounds=(0, None),
            doc="Total collector area needed",
        )

        self.number_collectors = Var(
            initialize=1,
            units=pyunits.dimensionless,
            bounds=(1, None),
            doc="Number of collectors needed",
        )

        self.storage_volume = Var(
            initialize=10,
            units=pyunits.m**3,
            bounds=(0, None),
            doc="Storage volume for flat plate system",
        )

        self.heat_load = Var(
            initialize=500,
            bounds=[100, 1000],
            units=pyunits.MW,
            doc="Rated plant heat capacity in MWt",
        )
        self.hours_storage = Var(
            initialize=20,
            bounds=[0, 26],
            units=pyunits.hour,
            doc="Rated plant hours of storage",
        )
        self.temperature_hot = Var(
            initialize=70,
            bounds=[50, 100],
            units=pyunits.K,
            doc="Hot outlet temperature",
        )

        self.heat_annual = Var(
            initialize=1000,
            units=pyunits.kWh,
            doc="Annual heat generated by flat plate",
        )
        self.electricity_annual = Var(
            initialize=20,
            units=pyunits.kWh,
            doc="Annual electricity consumed by flat plate",
        )

        self.surrogate_inputs = [
            self.heat_load,
            self.hours_storage,
            self.temperature_hot,
        ]
        self.surrogate_outputs = [self.heat_annual, self.electricity_annual]

        self.input_labels = ["heat_load", "hours_storage", "temperature_hot"]
        self.output_labels = ["heat_annual", "electricity_annual"]

        if self.config.surrogate_model_file:
            self.surrogate_file = os.path.join(
                os.path.dirname(__file__), self.config.surrogate_model_file
            )

        else:
            self.dataset_filename = os.path.join(
                os.path.dirname(__file__), "data/flat_plate_data.pkl"
            )
            self.surrogate_file = os.path.join(
                os.path.dirname(__file__), "flat_plate_surrogate.json"
            )

        self._load_surrogate()

        self.heat_constraint = Constraint(
            expr=self.heat_annual
            == self.heat * pyunits.convert(1 * pyunits.year, to_units=pyunits.hour)
        )

        self.electricity_constraint = Constraint(
            expr=self.electricity_annual
            == self.electricity
            * pyunits.convert(1 * pyunits.year, to_units=pyunits.hour)
        )

        self.collector_area_total_constraint = Constraint(
            expr=self.collector_area_total
            * (self.FR_ta - self.FR_UL * self.factor_delta_T)
            == pyunits.convert(self.heat_load, to_units=pyunits.kilowatt)
        )

        self.number_collectors_constraint = Constraint(
            expr=self.number_collectors
            == self.collector_area_total / self.collector_area_per
        )

        self.storage_volume_constraint = Constraint(
            expr=self.storage_volume
            == pyunits.convert(
                (
                    (self.hours_storage * self.heat_load)
                    / (
                        self.specific_heat_water
                        * self.temperature_cold
                        * self.dens_water
                    )
                ),
                to_units=pyunits.m**3,
            )
        )

    def calculate_scaling_factors(self):

        if iscale.get_scaling_factor(self.hours_storage) is None:
            sf = iscale.get_scaling_factor(self.hours_storage, default=1)
            iscale.set_scaling_factor(self.hours_storage, sf)

        if iscale.get_scaling_factor(self.heat_load) is None:
            sf = iscale.get_scaling_factor(self.heat_load, default=1e-2, warning=True)
            iscale.set_scaling_factor(self.heat_load, sf)

        if iscale.get_scaling_factor(self.temperature_hot) is None:
            sf = iscale.get_scaling_factor(
                self.temperature_hot, default=1e-1, warning=True
            )
            iscale.set_scaling_factor(self.temperature_hot, sf)

        if iscale.get_scaling_factor(self.heat_annual) is None:
            sf = iscale.get_scaling_factor(self.heat_annual, default=1e-4, warning=True)
            iscale.set_scaling_factor(self.heat_annual, sf)

        if iscale.get_scaling_factor(self.heat) is None:
            sf = iscale.get_scaling_factor(self.heat, default=1e-4, warning=True)
            iscale.set_scaling_factor(self.heat, sf)

        if iscale.get_scaling_factor(self.electricity_annual) is None:
            sf = iscale.get_scaling_factor(
                self.electricity_annual, default=1e-3, warning=True
            )
            iscale.set_scaling_factor(self.electricity_annual, sf)

        if iscale.get_scaling_factor(self.electricity) is None:
            sf = iscale.get_scaling_factor(self.electricity, default=1e-3, warning=True)
            iscale.set_scaling_factor(self.electricity, sf)

        if iscale.get_scaling_factor(self.number_collectors) is None:
            sf = iscale.get_scaling_factor(
                self.number_collectors, default=1e-4, warning=True
            )
            iscale.set_scaling_factor(self.number_collectors, sf)

        if iscale.get_scaling_factor(self.collector_area_total) is None:
            sf = iscale.get_scaling_factor(
                self.collector_area_total, default=1e-6, warning=True
            )
            iscale.set_scaling_factor(self.collector_area_total, sf)

        if iscale.get_scaling_factor(self.storage_volume) is None:
            sf = iscale.get_scaling_factor(
                self.storage_volume, default=1e-4, warning=True
            )
            iscale.set_scaling_factor(self.storage_volume, sf)

    def initialize_build(
        self,
        outlvl=idaeslog.NOTSET,
        solver=None,
        optarg=None,
    ):
        """
        General wrapper for initialization routines

        Keyword Arguments:
            outlvl : sets output level of initialization routine
            optarg : solver options dictionary object (default=None)
            solver : str indicating which solver to use during
                     initialization (default = None)

        Returns: None
        """
        init_log = idaeslog.getInitLogger(self.name, outlvl, tag="unit")
        solve_log = idaeslog.getSolveLogger(self.name, outlvl, tag="unit")

        iscale.calculate_variable_from_constraint(
            self.heat_annual, self.surrogate_blk.pysmo_constraint["heat_annual"]
        )
        iscale.calculate_variable_from_constraint(
            self.heat_annual, self.heat_constraint
        )
        iscale.calculate_variable_from_constraint(
            self.electricity_annual,
            self.surrogate_blk.pysmo_constraint["electricity_annual"],
        )

        # Create solver
        opt = get_solver(solver, optarg)

        # Solve unit
        with idaeslog.solver_log(solve_log, idaeslog.DEBUG) as slc:
            res = opt.solve(self, tee=slc.tee)

        init_log.info_high(f"Initialization Step 2 {idaeslog.condition(res)}")

        if not check_optimal_termination(res):
            raise InitializationError(f"Unit model {self.name} failed to initialize")

        init_log.info("Initialization Complete: {}".format(idaeslog.condition(res)))
