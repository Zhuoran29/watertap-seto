import os
import math
from functools import partial
from collections import defaultdict
from pyomo.environ import (
    ConcreteModel,
    value,
    Set,
    Var,
    SolverFactory,
    Constraint,
    Objective,
    NonNegativeReals,
    TransformationFactory,
    Block,
    check_optimal_termination,
    units as pyunits,
)
from pyomo.util.check_units import assert_units_consistent
from pyomo.network import Arc

import idaes.logger as idaeslog
from idaes.core import FlowsheetBlock, UnitModelCostingBlock, MaterialFlowBasis
from idaes.core.util.initialization import propagate_state
from idaes.core.solvers import get_solver
from idaes.models.unit_models import Product, Feed, StateJunction, Separator
from idaes.core.util.scaling import *
from idaes.core.util.model_statistics import *

from watertap.unit_models.pressure_changer import Pump
from watertap.core.util.model_diagnostics.infeasible import *

from watertap.property_models.NaCl_prop_pack import NaClParameterBlock
from watertap.property_models.multicomp_aq_sol_prop_pack import MCASParameterBlock
from watertap.property_models.seawater_prop_pack import SeawaterParameterBlock
from watertap.core.zero_order_properties import WaterParameterBlock as ZOParameterBlock
from watertap.property_models.water_prop_pack import WaterParameterBlock
from watertap.property_models.unit_specific.cryst_prop_pack import (
    NaClParameterBlock as CrystallizerParameterBlock,
)

from watertap_contrib.reflo.costing import (
    TreatmentCosting,
    EnergyCosting,
    REFLOSystemCosting,
)
from watertap_contrib.reflo.core import REFLODatabase
from watertap_contrib.reflo.analysis.case_studies.KBHDP import *
from watertap_contrib.reflo.analysis.case_studies.KBHDP.components.FPC import *

_log = idaeslog.getLogger(__name__)

__all__ = [
    "build_md_system",
    "build_and_run_kbhdp_zld",
    "build_and_run_md_system",
    "build_and_run_mec_system",
    "set_md_operating_conditions",
    "init_md_system",
    "build_and_run_primary_treatment",
    "build_primary_system",
    "build_primary_treatment",
    "add_primary_connections",
    "add_primary_system_scaling",
    "set_primary_operating_conditions",
    "init_primary_treatment",
]


############################################################
############################################################
################### PRIMARY TREATMENT SYSTEM ###############
############################################################
############################################################


def build_and_run_primary_treatment(
    Qin=4,
    water_recovery=0.8,
    fixed_pressure=None,
    ro_mem_area=None,
    objective=None,
    **kwargs,
):

    m = build_primary_system()
    add_primary_connections(m)
    set_primary_operating_conditions(m, Qin=Qin)
    add_primary_system_scaling(m)
    init_primary_system(m)
    optimize(
        m,
        water_recovery=water_recovery,
        fixed_pressure=fixed_pressure,
        ro_mem_area=ro_mem_area,
        objective=objective,
    )
    m.fs.brine.properties[0].conc_mass_phase_comp
    m.fs.brine.properties[0].flow_vol_phase
    m.fs.brine.flow_mgd = Expression(
        expr=pyunits.convert(
            m.fs.brine.properties[0].flow_vol_phase["Liq"],
            to_units=pyunits.Mgallons / pyunits.day,
        )
    )
    results = solve(m)

    return m


def build_primary_system():
    """
    Build primary treatment system flowsheet.
    """
    m = ConcreteModel()
    m.db = REFLODatabase()
    m.fs = FlowsheetBlock(dynamic=False)

    m.fs.MCAS_properties = MCASParameterBlock(
        solute_list=[
            "Alkalinity_2-",
            "Ca_2+",
            "Cl_-",
            "Mg_2+",
            "K_+",
            "SiO2",
            "Na_+",
            "SO2_-4+",
        ],
        material_flow_basis=MaterialFlowBasis.mass,
    )

    m.fs.RO_properties = NaClParameterBlock()
    m.fs.ZO_properties = ZOParameterBlock(solute_list=["tds", "tss"])
    m.fs.MD_properties = SeawaterParameterBlock()

    build_primary_treatment(m)

    m.fs.water_recovery = Var(
        initialize=0.5,
        bounds=(0, 0.99),
        domain=NonNegativeReals,
        units=pyunits.dimensionless,
        doc="System Water Recovery",
    )

    m.fs.eq_water_recovery = Constraint(
        expr=m.fs.feed.properties[0].flow_vol * m.fs.water_recovery
        == m.fs.product.properties[0].flow_vol
    )

    add_primary_treatment_costing(m)

    return m


def build_primary_treatment(m):
    """
    Build treatment train through RO.
    """
    # treatment = m.fs.treatment = Block()

    m.fs.feed = Feed(property_package=m.fs.MCAS_properties)
    m.fs.product = Product(property_package=m.fs.RO_properties)
    m.fs.sludge = Product(property_package=m.fs.ZO_properties)
    m.fs.UF_waste = Product(property_package=m.fs.ZO_properties)
    m.fs.brine = Product(property_package=m.fs.MD_properties)

    m.fs.EC = FlowsheetBlock(dynamic=False)
    m.fs.UF = FlowsheetBlock(dynamic=False)
    m.fs.pump = Pump(property_package=m.fs.RO_properties)
    m.fs.RO = FlowsheetBlock(dynamic=False)

    m.fs.MCAS_to_TDS_translator = Translator_MCAS_to_TDS(
        inlet_property_package=m.fs.MCAS_properties,
        outlet_property_package=m.fs.ZO_properties,
        has_phase_equilibrium=False,
        outlet_state_defined=False,
    )

    m.fs.TDS_to_NaCl_translator = Translator_TDS_to_NACL(
        inlet_property_package=m.fs.ZO_properties,
        outlet_property_package=m.fs.RO_properties,
        has_phase_equilibrium=False,
        outlet_state_defined=True,
    )

    m.fs.RO_brine_to_MD_translator = Translator_NaCl_to_TDS(
        inlet_property_package=m.fs.RO_properties,
        outlet_property_package=m.fs.MD_properties,
        has_phase_equilibrium=False,
        outlet_state_defined=False,
    )

    build_ec(m, m.fs.EC, prop_package=m.fs.ZO_properties)
    build_UF(m, m.fs.UF, prop_package=m.fs.ZO_properties)
    build_ro(m, m.fs.RO, prop_package=m.fs.RO_properties)


def add_primary_connections(m):
    # treatment = m.fs.treatment

    m.fs.feed_to_translator = Arc(
        source=m.fs.feed.outlet,
        destination=m.fs.MCAS_to_TDS_translator.inlet,
    )

    m.fs.translator_to_EC = Arc(
        source=m.fs.MCAS_to_TDS_translator.outlet,
        destination=m.fs.EC.feed.inlet,
    )

    m.fs.EC_to_UF = Arc(
        source=m.fs.EC.product.outlet,
        destination=m.fs.UF.feed.inlet,
    )

    m.fs.EC_to_sludge = Arc(
        source=m.fs.EC.disposal.outlet,
        destination=m.fs.sludge.inlet,
    )

    m.fs.UF_to_translator3 = Arc(
        source=m.fs.UF.product.outlet,
        destination=m.fs.TDS_to_NaCl_translator.inlet,
    )

    m.fs.UF_to_waste = Arc(
        source=m.fs.UF.disposal.outlet,
        destination=m.fs.UF_waste.inlet,
    )

    m.fs.translator_to_pump = Arc(
        source=m.fs.TDS_to_NaCl_translator.outlet,
        destination=m.fs.pump.inlet,
    )

    m.fs.pump_to_ro = Arc(
        source=m.fs.pump.outlet,
        destination=m.fs.RO.feed.inlet,
    )

    m.fs.ro_to_product = Arc(
        source=m.fs.RO.product.outlet,
        destination=m.fs.product.inlet,
    )

    m.fs.ro_to_brine_translator = Arc(
        source=m.fs.RO.disposal.outlet,
        destination=m.fs.RO_brine_to_MD_translator.inlet,
    )

    m.fs.brine_translator_to_brine = Arc(
        source=m.fs.RO_brine_to_MD_translator.outlet,
        destination=m.fs.brine.inlet,
    )

    TransformationFactory("network.expand_arcs").apply_to(m)


def set_treatment_scaling(m):

    # set default scaling for MCAS
    m.fs.MCAS_properties.set_default_scaling(
        "flow_mass_phase_comp", 10**-1, index=("Liq", "H2O")
    )
    m.fs.MCAS_properties.set_default_scaling(
        "flow_mass_phase_comp", 10**-1, index=("Liq", "NaCl")
    )

    # set default scaling for ZO
    m.fs.ZO_properties.set_default_scaling("flow_mass_comp", 1e-2, index=("H2O"))
    m.fs.ZO_properties.set_default_scaling("flow_mass_comp", 1, index=("tds"))
    m.fs.ZO_properties.set_default_scaling("flow_mass_comp", 1e5, index=("tss"))

    # set default scaling for SW
    m.fs.RO_properties.set_default_scaling(
        "flow_mass_phase_comp", 1, index=("Liq", "H2O")
    )
    m.fs.RO_properties.set_default_scaling(
        "flow_mass_phase_comp", 1e2, index=("Liq", "NaCl")
    )


def add_constraints(m):
    treatment = m.fs.treatment

    m.fs.water_recovery = Var(
        initialize=0.5,
        bounds=(0, 0.99),
        domain=NonNegativeReals,
        units=pyunits.dimensionless,
        doc="System Water Recovery",
    )

    m.fs.eq_water_recovery = Constraint(
        expr=treatment.feed.properties[0].flow_vol * m.fs.water_recovery
        == treatment.product.properties[0].flow_vol
    )


def add_primary_treatment_costing(m):

    m.fs.costing = TreatmentCosting()
    m.fs.costing.electricity_cost.fix(
        pyunits.convert(
            0.066 * pyunits.USD_2023 / pyunits.kWh,
            to_units=pyunits.USD_2018 / pyunits.kWh,
        )
    )
    m.fs.costing.heat_cost.fix(0.01660)

    m.fs.pump.costing = UnitModelCostingBlock(
        flowsheet_costing_block=m.fs.costing,
    )

    add_ec_costing(m, m.fs.EC, m.fs.costing)
    add_UF_costing(m, m.fs.UF, m.fs.costing)
    add_ro_costing(m, m.fs.RO, m.fs.costing)

    m.fs.costing.cost_process()
    m.fs.costing.add_LCOW(m.fs.product.properties[0].flow_vol_phase["Liq"])


def add_primary_system_scaling(m):
    set_treatment_scaling(m)
    add_ec_scaling(m, m.fs.EC)
    add_UF_scaling(m.fs.UF)
    add_ro_scaling(m, m.fs.RO)
    calculate_scaling_factors(m)


def set_inlet_conditions(
    m,
    Qin=None,
    supply_pressure=101325,
):

    print(f'\n{"=======> SETTING OPERATING CONDITIONS <=======":^60}\n')

    # treatment = m.fs.treatment

    # Convert Q_in from MGD to kg/s
    Qin = pyunits.convert(
        Qin * pyunits.Mgallon * pyunits.day**-1, to_units=pyunits.m**3 / pyunits.s
    )
    feed_density = 1000 * pyunits.kg / pyunits.m**3
    print('\n=======> SETTING FEED CONDITIONS <======="\n')
    print(f"Flow Rate: {value(Qin):<10.2f}{pyunits.get_units(Qin)}")

    if Qin is None:
        m.fs.feed.properties[0].flow_mass_phase_comp["Liq", "H2O"].fix(1)
    else:
        m.fs.feed.properties[0].flow_mass_phase_comp["Liq", "H2O"].fix(
            Qin * feed_density
        )

    inlet_dict = {
        "Ca_2+": 0.61 * pyunits.kg / pyunits.m**3,
        "Mg_2+": 0.161 * pyunits.kg / pyunits.m**3,
        "Alkalinity_2-": 0.0821 * pyunits.kg / pyunits.m**3,
        "SiO2": 0.13 * pyunits.kg / pyunits.m**3,
        "Cl_-": 5.5 * pyunits.kg / pyunits.m**3,
        "Na_+": 5.5 * pyunits.kg / pyunits.m**3,
        "K_+": 0.016 * pyunits.kg / pyunits.m**3,
        "SO2_-4+": 0.23 * pyunits.kg / pyunits.m**3,
    }

    for solute, solute_conc in inlet_dict.items():
        m.fs.feed.properties[0].flow_mass_phase_comp["Liq", solute].fix(
            pyunits.convert(
                (
                    m.fs.feed.properties[0].flow_mass_phase_comp["Liq", "H2O"]
                    / (1000 * pyunits.kg / pyunits.m**3)
                )
                * solute_conc,
                to_units=pyunits.kg / pyunits.s,
            )
        )
        m.fs.MCAS_properties.set_default_scaling(
            "flow_mass_phase_comp",
            1 / value(m.fs.feed.properties[0].flow_mass_phase_comp["Liq", solute]),
            index=("Liq", solute),
        )
    m.fs.MCAS_properties.set_default_scaling(
        "flow_mass_phase_comp",
        1 / value(m.fs.feed.properties[0].flow_mass_phase_comp["Liq", "H2O"]),
        index=("Liq", "H2O"),
    )

    feed_temperature = 273.15 + 20

    # # initialize feed
    m.fs.feed.pressure[0].fix(supply_pressure)
    m.fs.feed.temperature[0].fix(feed_temperature)


def set_primary_operating_conditions(m, Qin=4, RO_pressure=20e5, **kwargs):
    # treatment = m.fs.treatment
    pump_efi = 0.8  # pump efficiency [-]
    # Set inlet conditions and operating conditions for each unit
    set_inlet_conditions(m, Qin=Qin)
    set_ec_operating_conditions(m, m.fs.EC)
    set_UF_op_conditions(m.fs.UF)
    m.fs.pump.efficiency_pump.fix(pump_efi)
    m.fs.pump.control_volume.properties_out[0].pressure.fix(RO_pressure)
    set_ro_system_operating_conditions(m, m.fs.RO, mem_area=10000)


def init_primary_treatment(m, verbose=True, solver=None):
    if solver is None:
        solver = get_solver()

    optarg = solver.options
    # treatment = m.fs.treatment

    print("\n\n-------------------- INITIALIZING SYSTEM --------------------\n\n")
    print(f"System Degrees of Freedom: {degrees_of_freedom(m)}")
    # assert_no_degrees_of_freedom(m)
    m.fs.feed.initialize(optarg=optarg)
    propagate_state(m.fs.feed_to_translator)

    m.fs.MCAS_to_TDS_translator.initialize(optarg=optarg)
    propagate_state(m.fs.translator_to_EC)

    init_ec(m, m.fs.EC)
    propagate_state(m.fs.EC_to_UF)

    init_UF(m, m.fs.UF)
    propagate_state(m.fs.UF_to_translator3)
    propagate_state(m.fs.UF_to_waste)

    m.fs.TDS_to_NaCl_translator.initialize(optarg=optarg)
    propagate_state(m.fs.translator_to_pump)

    m.fs.pump.initialize(optarg=optarg)

    propagate_state(m.fs.pump_to_ro)

    init_ro_system(m, m.fs.RO)
    propagate_state(m.fs.ro_to_product)
    m.fs.product.initialize()
    propagate_state(m.fs.ro_to_brine_translator)

    m.fs.RO_brine_to_MD_translator.initialize()

    propagate_state(m.fs.brine_translator_to_brine)

    m.fs.brine.initialize()
    print(f"System Degrees of Freedom: {degrees_of_freedom(m)}")

    # display_system_stream_table(m)


def init_primary_system(m, verbose=True, solver=None):
    print(f'\n{"=======> SYSTEM INITIALIZATION <=======":^60}\n')
    print(f"System Degrees of Freedom: {degrees_of_freedom(m)}")
    init_primary_treatment(m)

    print(f"System Degrees of Freedom: {degrees_of_freedom(m)}")


############################################################
############################################################
################### MD  SYSTEM #############################
############################################################
############################################################


def build_and_run_md_system(Qin=1, Cin=60, water_recovery=0.5):

    m = build_md_system(Qin=Qin, Cin=Cin, water_recovery=water_recovery)
    set_md_operating_conditions(m)
    init_md_system(m)

    results = solve(m, tee=False)

    m.fs.feed.properties[0].flow_vol_phase
    m.fs.md.feed.properties[0].flow_vol_phase
    m.fs.disposal.properties[0].flow_vol_phase
    m.fs.disposal.properties[0].conc_mass_phase_comp
    m.fs.disposal.flow_mgd = Expression(
        expr=pyunits.convert(
            m.fs.disposal.properties[0].flow_vol_phase["Liq"],
            to_units=pyunits.Mgallons / pyunits.day,
        )
    )

    m.fs.md.unit.add_costing_module(m.fs.costing)

    m.fs.costing.cost_process()
    m.fs.costing.initialize()

    m.fs.costing.add_annual_water_production(
        m.fs.product.properties[0].flow_vol_phase["Liq"]
    )
    m.fs.costing.add_LCOW(m.fs.product.properties[0].flow_vol_phase["Liq"])

    print("\nMD System Degrees of Freedom:", degrees_of_freedom(m), "\n")

    assert degrees_of_freedom(m) == 0

    results = solve(m)

    return m


def build_md_system(Qin=4, Cin=12, water_recovery=0.5):

    m = ConcreteModel()
    m.fs = FlowsheetBlock(dynamic=False)
    m.fs.costing = TreatmentCosting()

    m.fs.costing.electricity_cost.fix(
        pyunits.convert(
            0.066 * pyunits.USD_2023 / pyunits.kWh,
            to_units=pyunits.USD_2018 / pyunits.kWh,
        )
    )
    m.fs.costing.heat_cost.fix(0.01660)

    m.inlet_flow_rate = pyunits.convert(
        Qin * pyunits.Mgallons / pyunits.day, to_units=pyunits.m**3 / pyunits.s
    )
    m.inlet_salinity = pyunits.convert(
        Cin * pyunits.g / pyunits.liter, to_units=pyunits.kg / pyunits.m**3
    )
    m.water_recovery = water_recovery

    # Property package
    m.fs.properties = SeawaterParameterBlock()

    # Create feed, product and concentrate state blocks
    m.fs.feed = Feed(property_package=m.fs.properties)
    m.fs.product = Product(property_package=m.fs.properties)
    m.fs.disposal = Product(property_package=m.fs.properties)

    # Create MD unit model at flowsheet level
    m.fs.md = FlowsheetBlock(dynamic=False)
    build_md(m, m.fs.md, prop_package=m.fs.properties)

    m.fs.feed_to_md = Arc(source=m.fs.feed.outlet, destination=m.fs.md.feed.inlet)

    m.fs.md_to_product = Arc(
        source=m.fs.md.permeate.outlet, destination=m.fs.product.inlet
    )

    m.fs.md_to_disposal = Arc(
        source=m.fs.md.concentrate.outlet, destination=m.fs.disposal.inlet
    )

    TransformationFactory("network.expand_arcs").apply_to(m)

    return m


def set_md_operating_conditions(m):
    feed_flow_rate = m.fs.md.model_input["feed_flow_rate"]
    feed_salinity = m.fs.md.model_input["feed_salinity"]
    feed_temp = m.fs.md.model_input["feed_temp"]

    # m.fs.feed.properties.calculate_state(
    #     var_args={
    #         ("flow_vol_phase", "Liq"): pyunits.convert(
    #             feed_flow_rate * pyunits.L / pyunits.h,
    #             to_units=pyunits.m**3 / pyunits.s,
    #         ),
    #         ("conc_mass_phase_comp", ("Liq", "TDS")): feed_salinity,
    #         ("temperature", None): feed_temp + 273.15,
    #         ("pressure", None): 101325,
    #     },
    #     hold_state=True,
    # )

    m.fs.feed.properties.calculate_state(
        var_args={
            ("flow_vol_phase", "Liq"): m.inlet_flow_rate,
            ("conc_mass_phase_comp", ("Liq", "TDS")): m.inlet_salinity,
            ("temperature", None): feed_temp + 273.15,
            ("pressure", None): 101325,
        },
        hold_state=True,
    )


def init_md_system(m, solver=None):
    if solver is None:
        solver = get_solver()

    optarg = solver.options

    print(
        "\n\n-------------------- INITIALIZING MEMBRANE DISTILLATION --------------------\n\n"
    )
    print(f"System Degrees of Freedom: {degrees_of_freedom(m)}")
    print("\n\n")

    m.fs.feed.initialize()
    propagate_state(m.fs.feed_to_md)

    init_md(m, m.fs.md, verbose=True, solver=None)

    propagate_state(m.fs.md_to_product)
    m.fs.product.initialize()

    propagate_state(m.fs.md_to_disposal)
    m.fs.disposal.initialize()


############################################################
############################################################
################### MEC SYSTEM #############################
############################################################
############################################################


def build_and_run_mec_system(
    Qin=None,
    Cin=None,
    number_effects=4,
    mec_kwargs=dict(),
):
    m = build_mec_system(number_effects=number_effects)
    set_mec_operating_conditions(m, Qin=Qin, tds=Cin, **mec_kwargs)
    init_mec_system(m)
    results = solve(m)
    # display_mec_streams(m, m.fs.MEC)
    return m


def build_mec_system(number_effects=4):
    m = ConcreteModel()
    m.fs = FlowsheetBlock(dynamic=False)
    m.fs.costing = TreatmentCosting()

    m.fs.costing.electricity_cost.fix(
        pyunits.convert(
            0.066 * pyunits.USD_2023 / pyunits.kWh,
            to_units=pyunits.USD_2018 / pyunits.kWh,
        )
    )
    m.fs.costing.heat_cost.fix(0.01660)
    
    m.fs.properties = CrystallizerParameterBlock()
    m.fs.vapor_properties = WaterParameterBlock()

    m.fs.feed = Feed(property_package=m.fs.properties)
    m.fs.product = Product(property_package=m.fs.properties)
    m.fs.solids = Product(property_package=m.fs.properties)

    m.fs.MEC = mec = FlowsheetBlock(dynamic=False)

    build_mec(m, m.fs.MEC, number_effects=number_effects)

    m.fs.feed_to_unit = Arc(source=m.fs.feed.outlet, destination=mec.unit.inlet)

    m.fs.mec_to_product = Arc(source=mec.product.outlet, destination=m.fs.product.inlet)

    m.fs.mec_to_solids = Arc(source=mec.solids.outlet, destination=m.fs.solids.inlet)

    TransformationFactory("network.expand_arcs").apply_to(m)

    return m


def set_mec_operating_conditions(
    m,
    Qin=None,  # MGD
    tds=None,  # g/L
    flow_mass_water=None,
    flow_mass_tds=None,
    operating_pressures=[0.45, 0.25, 0.208, 0.095],
    crystallizer_yield=0.5,
    saturated_steam_pressure_gage=3,
    heat_transfer_coefficient=0.1,
    **kwargs,
):
    atm_pressure = 101325 * pyunits.Pa
    rho = 1000 * pyunits.kg / pyunits.m**3

    saturated_steam_pressure = atm_pressure + pyunits.convert(
        saturated_steam_pressure_gage * pyunits.bar, to_units=pyunits.Pa
    )

    m.operating_pressures = operating_pressures
    m.crystallizer_yield = crystallizer_yield
    m.heat_transfer_coefficient = heat_transfer_coefficient
    m.saturated_steam_pressure = saturated_steam_pressure
    m.saturated_steam_pressure_gage = saturated_steam_pressure_gage

    if flow_mass_water is None:
        Qin = Qin * pyunits.Mgallons / pyunits.day
        m.flow_mass_water = pyunits.convert(Qin * rho, to_units=pyunits.kg / pyunits.s)
        m.fs.feed.properties[0].flow_mass_phase_comp["Liq", "H2O"].fix(
            m.flow_mass_water
        )
    else:
        m.flow_mass_water = flow_mass_water
        m.fs.feed.properties[0].flow_mass_phase_comp["Liq", "H2O"].fix(flow_mass_water)
    if flow_mass_tds is None:
        tds = tds * pyunits.gram / pyunits.liter
        m.flow_mass_tds = pyunits.convert(Qin * tds, to_units=pyunits.kg / pyunits.s)
        m.fs.feed.properties[0].flow_mass_phase_comp["Liq", "NaCl"].fix(m.flow_mass_tds)
    else:
        m.flow_mass_tds = flow_mass_tds
        m.fs.feed.properties[0].flow_mass_phase_comp["Liq", "NaCl"].fix(flow_mass_tds)

    m.fs.feed.properties[0].flow_mass_phase_comp["Sol", "NaCl"].fix(0)
    m.fs.feed.properties[0].temperature.fix(298.15)
    m.fs.feed.properties[0].pressure.fix(101325)
    m.fs.feed.properties[0].flow_vol_phase["Liq"]


def init_mec_system(m):
    m.fs.feed.properties[0].conc_mass_phase_comp
    m.fs.feed.initialize()
    propagate_state(m.fs.feed_to_unit)

    init_mec(m, m.fs.MEC)

    propagate_state(m.fs.MEC.unit_to_product)
    m.fs.MEC.product.initialize()

    propagate_state(m.fs.mec_to_product)
    m.fs.product.initialize()

    propagate_state(m.fs.MEC.unit_to_solids)
    m.fs.MEC.solids.initialize()

    propagate_state(m.fs.mec_to_solids)
    m.fs.solids.initialize()

    add_mec_costing(m, m.fs.MEC)
    m.fs.costing.cost_process()
    m.fs.costing.add_LCOW(m.fs.product.properties[0].flow_vol_phase["Liq"])
    m.fs.costing.initialize()


############################################################
############################################################
############################################################
############################################################


def solve(m, solver=None, tee=True, raise_on_failure=True, debug=False):
    # ---solving---
    if solver is None:
        solver = get_solver()
        solver.options["max_iter"] = 2000

    print("\n--------- SOLVING ---------\n")

    results = solver.solve(m, tee=tee)

    if check_optimal_termination(results):
        print("\n--------- OPTIMAL SOLVE!!! ---------\n")
        if debug:
            print("\n--------- CHECKING JACOBIAN ---------\n")
            print("\n--------- TREATMENT ---------\n")
            check_jac(m.fs.treatment)
            print("\n--------- ENERGY ---------\n")
            check_jac(m.fs.energy)

            print("\n--------- CLOSE TO BOUNDS ---------\n")
            print_close_to_bounds(m)
        return results
    msg = (
        "The current configuration is infeasible. Please adjust the decision variables."
    )
    if raise_on_failure:
        print('\n{"=======> INFEASIBLE BOUNDS <=======":^60}\n')
        print_infeasible_bounds(m)
        print('\n{"=======> INFEASIBLE CONSTRAINTS <=======":^60}\n')
        print_infeasible_constraints(m)
        print('\n{"=======> CLOSE TO BOUNDS <=======":^60}\n')
        print_close_to_bounds(m)

        raise RuntimeError(msg)
    else:
        print("\n--------- FAILED SOLVE!!! ---------\n")
        print(msg)
        assert False


def optimize(
    m,
    water_recovery=0.5,
    fixed_pressure=None,
    ro_mem_area=None,
    objective="LCOW",
):
    # treatment = m.fs.treatment
    print("\n\nDOF before optimization: ", degrees_of_freedom(m))

    if objective == "LCOW":
        m.fs.lcow_objective = Objective(expr=m.fs.costing.LCOW)
    else:
        m.fs.membrane_area_objective = Objective(expr=m.fs.RO.stage[1].module.area)

    if water_recovery is not None:
        print(f"\n------- Fixed Recovery at {100*water_recovery}% -------")
        m.fs.water_recovery.fix(water_recovery)
    else:
        lower_bound = 0.01
        upper_bound = 0.99
        print(f"\n------- Unfixed Recovery -------")
        print(f"Lower Bound: {lower_bound}")
        print(f"Upper Bound: {upper_bound}")
        m.fs.water_recovery.unfix()
        m.fs.water_recovery.setlb(0.01)
        m.fs.water_recovery.setub(0.99)

    if fixed_pressure is not None:
        print(f"\n------- Fixed RO Pump Pressure at {fixed_pressure} -------\n")
        m.fs.pump.control_volume.properties_out[0].pressure.fix(fixed_pressure)
    else:
        lower_bound = 100 * pyunits.psi
        upper_bound = 900 * pyunits.psi
        print(f"------- Unfixed RO Pump Pressure -------")
        print(f"Lower Bound: {value(lower_bound)} {pyunits.get_units(lower_bound)}")
        print(f"Upper Bound: {value(upper_bound)} {pyunits.get_units(upper_bound)}")
        m.fs.pump.control_volume.properties_out[0].pressure.unfix()
        m.fs.pump.control_volume.properties_out[0].pressure.setlb(lower_bound)
        m.fs.pump.control_volume.properties_out[0].pressure.setub(upper_bound)

    if ro_mem_area is not None:
        print(f"\n------- Fixed RO Membrane Area at {ro_mem_area} -------\n")
        for idx, stage in m.fs.RO.stage.items():
            stage.module.area.fix(ro_mem_area)
    else:
        lower_bound = 1e3
        upper_bound = 2e5
        print(f"\n------- Unfixed RO Membrane Area -------")
        print(f"Lower Bound: {lower_bound} m2")
        print(f"Upper Bound: {upper_bound} m2")
        print("\n")
        for idx, stage in m.fs.RO.stage.items():
            stage.module.area.unfix()
            stage.module.area.setub(1e6)


############################################################
############################################################
#################### FULL ZLD TRAIN ########################
############################################################
############################################################


def build_and_run_kbhdp_zld(primary_kwargs=dict()):

    m1 = build_and_run_primary_treatment(**primary_kwargs)

    flow_to_md = value(m1.fs.brine.flow_mgd)
    tds_to_md = value(m1.fs.brine.properties[0].conc_mass_phase_comp["Liq", "TDS"])

    m_md = build_and_run_md_system(Qin=flow_to_md, Cin=tds_to_md)

    flow_to_mec = value(m_md.fs.disposal.flow_mgd)
    tds_to_mec = value(
        m_md.fs.disposal.properties[0].conc_mass_phase_comp["Liq", "TDS"]
    )

    flow_mass_water = value(
        m_md.fs.disposal.properties[0].flow_mass_phase_comp["Liq", "H2O"]
    )
    flow_mass_tds = value(
        m_md.fs.disposal.properties[0].flow_mass_phase_comp["Liq", "TDS"]
    )
    mec_kwargs = dict(flow_mass_water=flow_mass_water, flow_mass_tds=flow_mass_tds)

    m_mec = build_and_run_mec_system(mec_kwargs=mec_kwargs)

    # m_mec.fs.costing.display()

    m_agg = build_agg_model(m1, m_md, m_mec)

    print(f" dof = {degrees_of_freedom(m_agg)}")
    solver = SolverFactory("ipopt")
    results = solver.solve(m_agg)
    print(f"termination {results.solver.termination_condition}")
    m_agg.fs.costing.display()
    m_agg.fs.costing.LCOW.display()

    return m1, m_md, m_mec



def build_agg_costing_blk(
    b,
    models=None,
    base_currency=None,
    base_period=pyunits.year,
    inlet_flow=None,
    product_flow=None,
    disposal_flow=None,
):
    if base_currency is None:
        base_currency = pyunits.USD_2023

    b.models = models
    b.base_currency = base_currency
    b.base_period = base_period
    b.capital_recovery_factor = (
        value(models[0].fs.costing.capital_recovery_factor) * b.base_period**-1
    )
    b.electricity_cost = value(models[0].fs.costing.electricity_cost)
    b.heat_cost = value(models[0].fs.costing.heat_cost)

    for m in models:
        assert value(m.fs.costing.capital_recovery_factor) == value(
            b.capital_recovery_factor
        )
        assert value(m.fs.costing.electricity_cost) == b.electricity_cost
        assert value(m.fs.costing.heat_cost) == b.heat_cost

    b.registered_unit_costing = Set()
    b.flow_types = Set()
    b.used_flows = Set()
    b.registered_flows = defaultdict(list)
    b.registered_flow_costs = defaultdict(list)
    b.defined_flows = defaultdict(list)

    for m in models:
        m.fs.costing.total_capital_cost.display()
        agg_flow_cost = getattr(m.fs.costing, "aggregate_flow_costs")
        # agg_flow_cost.display()

        for f in m.fs.costing.flow_types:
            if f not in b.flow_types:
                b.flow_types.add(f)

        for f in m.fs.costing.used_flows:

            if f not in b.used_flows:
                b.used_flows.add(f)

            if f in m.fs.costing._registered_flows.keys():
                b.registered_flows[f].extend(m.fs.costing._registered_flows[f])
                b.registered_flow_costs[f].append(value(agg_flow_cost[f]))
        for e in m.fs.costing._registered_unit_costing:
            b.registered_unit_costing.add(e)

    b.total_capital_cost = Var(initialize=1e6, units=b.base_currency)

    @b.Constraint()
    def eq_total_capital_cost(blk):
        return blk.total_capital_cost == sum(
            value(m.fs.costing.total_capital_cost) for m in models
        )

    # b.total_capital_cost_constraint = Constraint(
    #     expr=
    # )
    b.total_operating_cost = Var(initialize=1e4, units=b.base_currency / b.base_period)

    @b.Constraint()
    def eq_total_operating_cost(blk):
        return blk.total_operating_cost == sum(
            value(m.fs.costing.total_operating_cost) for m in models
        )

    b.aggregate_flow_costs = Var(b.used_flows)

    @b.Constraint(b.used_flows)
    def eq_aggregate_flow_cost(blk, f):
        return blk.aggregate_flow_costs[f] == value(sum(blk.registered_flow_costs[f]))

    def agg_flow_rule(blk, f, funits):
        e = 0
        for flow in blk.registered_flows[f]:
            e += value(pyo.units.convert(flow, to_units=funits))
        agg_flow = getattr(blk, f"aggregate_flow_{f}")
        return agg_flow == e

    for k, f in b.registered_flows.items():
        funits = pyunits.get_units(f[0])
        agg_var = Var(units=funits, doc=f"Aggregate flow for {k}")
        b.add_component(f"aggregate_flow_{k}", agg_var)
        agg_const = Constraint(rule=partial(agg_flow_rule, f=k, funits=funits))
        b.add_component(f"aggregate_flow_{k}_constraint", agg_const)

    @b.Expression()
    def LCOW(blk):
        numerator = (
            blk.total_capital_cost * blk.capital_recovery_factor
            + blk.total_operating_cost
        )
        denominator = pyunits.convert(
            product_flow * pyunits.m**3 / pyunits.s,
            to_units=pyunits.m**3 / b.base_period,
        )
        return pyunits.convert(
            numerator / denominator, to_units=b.base_currency / pyunits.m**3
        )


def build_agg_model(m1, m_md, m_mec):

    m1.name = "primary"
    m_md.name = "MD"
    m_mec.name = "MEC"

    m = ConcreteModel()
    m.fs = FlowsheetBlock(dynamic=False)

    m.fs.feed_flows = [m1.fs.feed.properties[0].flow_vol_phase["Liq"]]
    m.fs.product_flows = [
        m1.fs.product.properties[0].flow_vol_phase["Liq"],
        m_md.fs.product.properties[0].flow_vol_phase["Liq"],
        m_mec.fs.product.properties[0].flow_vol_phase["Liq"],
    ]

    m.fs.costing = Block()

    m.fs.flow_in = Expression(expr=sum(value(f) for f in m.fs.feed_flows))
    m.fs.flow_treated = Expression(expr=sum(value(f) for f in m.fs.product_flows))
    m.fs.flow_waste = Expression(expr=m.fs.flow_in - m.fs.flow_treated)
    m.fs.system_recovery = Expression(expr=m.fs.flow_treated / m.fs.flow_in)

    build_agg_costing_blk(
        m.fs.costing,
        models=[m1, m_md, m_mec],
        # inlet_flows=m.fs.inlet_flows,
        product_flow=m.fs.flow_treated,
    )


    return m



def build_energy(m):
    energy = m.fs.energy = Block()
    build_pv(m)
    build_fpc_mid(m)


def add_energy_costing(m):
    energy = m.fs.energy
    energy.costing = EnergyCosting()
    elec_cost = pyunits.convert(0.066 * pyunits.USD_2023, to_units=pyunits.USD_2018)()
    m.fs.energy.costing.electricity_cost.fix(elec_cost)

    energy.pv.costing = UnitModelCostingBlock(
        flowsheet_costing_block=energy.costing,
    )

    energy.costing.cost_process()
    energy.costing.initialize()


if __name__ == "__main__":

    m1, m_md, m_mec = build_and_run_kbhdp_zld()
    # build_and_run_mec_system()

    # m1.fs.water_recovery.display()
    # m_md.fs.disposal.properties[0].display()

    pass

    # _treatment()
    # m1.fs.water_recovery.display()
    # m1.fs.treatment.brine.properties[0].display()
    # m = build_md_system(Qin=value(m1.fs.treatment.brine.flow_mgd), Cin=value(m1.fs.treatment.brine.properties[0].conc_mass_phase_comp["Liq", "TDS"]))

    # # m = build_md_system(Qin=1, Cin=60)

    # set_md_operating_conditions(m)
    # init_md_system(m)

    # # m.fs.feed.properties[0].display()

    # results = solve(m, tee=False)

    # m.fs.feed.properties[0].flow_vol_phase
    # m.fs.md.feed.properties[0].flow_vol_phase
    # m.fs.disposal.properties[0].flow_vol_phase
    # m.fs.disposal.properties[0].conc_mass_phase_comp

    # m.fs.md.unit.add_costing_module(m.fs.costing)

    # m.fs.costing.cost_process()
    # m.fs.costing.initialize()

    # prod_flow = pyunits.convert(
    #     m.fs.product.properties[0].flow_vol_phase["Liq"],
    #     to_units=pyunits.m**3 / pyunits.day,
    # )

    # m.fs.costing.add_annual_water_production(prod_flow)
    # m.fs.costing.add_LCOW(prod_flow)
    # print("\nSystem Degrees of Freedom:", degrees_of_freedom(m), "\n")

    # assert degrees_of_freedom(m) == 0

    # results = solve(m)
    # print("\n--------- Cost solve Completed ---------\n")

    # print(
    #     "Inlet flow rate in m3/day:",
    #     value(
    #         pyunits.convert(
    #             m.fs.feed.properties[0].flow_vol_phase["Liq"],
    #             pyunits.m**3 / pyunits.day,
    #         )
    #     ),
    # )
    # report_MD(m, m.fs.md)
    # report_md_costing(m, m.fs)

    # print("\n")
    # print(
    #     f'Sys Feed Flow Rate: {value(pyunits.convert(m.fs.feed.properties[0].flow_vol_phase["Liq"], pyunits.m ** 3 / pyunits.day)):<10.2f} m3/day'
    # )
    # print(
    #     f'MD  Feed Flow Rate: {value(pyunits.convert(m.fs.md.feed.properties[0].flow_vol_phase["Liq"], pyunits.m ** 3 / pyunits.day)):<10.2f} m3/day'
    # )
    # print(
    #     f'Sys Perm Flow Rate: {value(pyunits.convert(m.fs.product.properties[0].flow_vol_phase["Liq"], pyunits.m ** 3 / pyunits.day)):<10.2f} m3/day'
    # )
    # print(
    #     f'MD  Perm Flow Rate: {value(pyunits.convert(m.fs.md.permeate.properties[0].flow_vol_phase["Liq"], pyunits.m ** 3 / pyunits.day)):<10.2f} m3/day'
    # )
    # print(
    #     f'Sys Conc Flow Rate: {value(pyunits.convert(m.fs.disposal.properties[0].flow_vol_phase["Liq"], pyunits.m ** 3 / pyunits.day)):<10.2f} m3/day'
    # )
    # print(
    #     f'MD  Conc Flow Rate: {value(pyunits.convert(m.fs.md.concentrate.properties[0].flow_vol_phase["Liq"], pyunits.m ** 3 / pyunits.day)):<10.2f} m3/day'
    # )
    # print(
    #     f'Calculated Recovery: {value(m.fs.md.permeate.properties[0].flow_vol_phase["Liq"] / (m.fs.md.permeate.properties[0].flow_vol_phase["Liq"] + m.fs.md.concentrate.properties[0].flow_vol_phase["Liq"])):<10.2f}'
    # )
    # # m1.fs.treatment.brine.properties[0].flow_vol_phase.display()
    # m.fs.feed.properties[0].flow_vol_phase.display()
    # m.fs.md.feed.properties[0].flow_vol_phase.display()
