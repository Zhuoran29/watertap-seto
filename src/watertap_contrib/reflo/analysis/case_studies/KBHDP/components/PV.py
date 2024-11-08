from pyomo.environ import (
    ConcreteModel,
    value,
    Constraint,
    units as pyunits,
)
from pyomo.util.check_units import assert_units_consistent
from idaes.core import FlowsheetBlock, UnitModelCostingBlock
from idaes.core.util.model_statistics import *
import idaes.core.util.scaling as iscale
from watertap.core.util.model_diagnostics.infeasible import *
from watertap.core.util.initialization import *
from watertap_contrib.reflo.solar_models.surrogate.pv import PVSurrogate
from watertap_contrib.reflo.costing import (
    TreatmentCosting,
    EnergyCosting,
    REFLOCosting,
    REFLOSystemCosting,
)

__all__ = [
    "build_pv",
    "train_pv_surrogate",
    "set_pv_constraints",
    "add_pv_scaling",
    "add_pv_costing_scaling",
    "print_PV_costing_breakdown",
    "report_PV",
]

def build_system():
    m = ConcreteModel()
    m.fs = FlowsheetBlock(dynamic=False)
    energy = m.fs.energy = Block()

    print(f'Degrees of Freedom: {degrees_of_freedom(m)}')
    return m


def build_pv(m):
    energy = m.fs.energy

    energy.pv = PVSurrogate(
        surrogate_model_file="/Users/zbinger/watertap-reflo/src/watertap_contrib/reflo/solar_models/surrogate/pv/pv_surrogate.json",
        dataset_filename="/Users/zbinger/watertap-reflo/src/watertap_contrib/reflo/solar_models/surrogate/pv/data/dataset.pkl",
        input_variables={
            "labels": ["design_size"],
            "bounds": {"design_size": [1, 200000]},
            "units": {"design_size": "kW"},
        },
        output_variables={
            "labels": ["annual_energy", "land_req"],
            "units": {"annual_energy": "kWh", "land_req": "acre"},
        },
        scale_training_data=False,
    )


def train_pv_surrogate(m):
    energy = m.fs.energy

    energy.pv.create_rbf_surrogate()

    assert False


def set_pv_constraints(m, focus="Size"):
    energy = m.fs.energy

    # m.fs.energy.pv.heat.fix(0)

    if focus == "Size":
        # energy.pv_design_constraint = Constraint(
        #     expr=m.fs.energy.pv.design_size
        #     == m.fs.treatment.costing.aggregate_flow_electricity
        # )
        m.fs.energy.pv.design_size.fix(1000)
    elif focus == "Energy":
        m.fs.energy.pv.annual_energy.fix(40000000)

    
    m.fs.energy.pv.load_surrogate()


def add_pv_costing(m, blk):
    energy = m.fs.energy
    energy.costing = EnergyCosting()

    energy.pv.costing = UnitModelCostingBlock(
        flowsheet_costing_block=energy.costing,
    )

    breakdown_dof(m)

def add_pv_scaling(m, blk):
    pv = blk

    iscale.set_scaling_factor(pv.design_size, 1e-4)
    iscale.set_scaling_factor(pv.annual_energy, 1e-8)
    iscale.set_scaling_factor(pv.electricity, 1e-7)


def add_pv_costing_scaling(m, blk):
    pv = blk

    iscale.set_scaling_factor(m.fs.energy.pv.costing.system_capacity, 1e-5)


def print_PV_costing_breakdown(pv):
    print(f'{"PV Capital Cost":<35s}{f"${value(pv.costing.capital_cost):<25,.0f}"}')
    print(
        f'{"PV Operating Cost":<35s}{f"${value(pv.costing.fixed_operating_cost):<25,.0f}"}'
    )


def report_PV(m):
    elec = "electricity"
    print(f"\n\n-------------------- PHOTOVOLTAIC SYSTEM --------------------\n\n")
    print(
        f'{"System Agg. Flow Electricity":<30s}{value(m.fs.treatment.costing.aggregate_flow_electricity):<10.1f}{"kW"}'
    )
    print(
        f'{"PV Agg. Flow Elec.":<30s}{value(m.fs.energy.pv.design_size):<10.1f}{pyunits.get_units(m.fs.energy.pv.design_size)}'
    )
    print(
        f'{"Treatment Agg. Flow Elec.":<30s}{value(m.fs.treatment.costing.aggregate_flow_electricity):<10.1f}{"kW"}'
    )
    print(
        f'{"Land Requirement":<30s}{value(m.fs.energy.pv.land_req):<10.1f}{pyunits.get_units(m.fs.energy.pv.land_req)}'
    )
    print(
        f'{"PV Annual Energy":<30s}{value(m.fs.energy.pv.annual_energy):<10,.0f}{pyunits.get_units(m.fs.energy.pv.annual_energy)}'
    )
    print(
        f'{"Treatment Annual Energy":<30s}{value(m.fs.annual_treatment_energy):<10,.0f}{"kWh/yr"}'
    )
    print("\n")
    print(
        f'{"PV Annual Generation":<25s}{f"{pyunits.convert(-1*m.fs.energy.pv.electricity, to_units=pyunits.kWh/pyunits.year)():<25,.0f}"}{"kWh/yr":<10s}'
    )
    print(
        f'{"Treatment Annual Demand":<25s}{f"{pyunits.convert(m.fs.treatment.costing.aggregate_flow_electricity, to_units=pyunits.kWh/pyunits.year)():<25,.0f}"}{"kWh/yr":<10s}'
    )
    # print(f'{"Energy Balance":<25s}{f"{value(m.fs.energy_balance):<25,.2f}"}')
    print(
        f'{"Treatment Elec Cost":<25s}{f"${value(m.fs.treatment.costing.aggregate_flow_costs[elec]):<25,.0f}"}{"$/yr":<10s}'
    )
    print(
        f'{"Energy Elec Cost":<25s}{f"${value(m.fs.energy.costing.aggregate_flow_costs[elec]):<25,.0f}"}{"$/yr":<10s}'
    )
    print("\nEnergy Balance")
    print(
        f'{"Treatment Agg. Flow Elec.":<30s}{value(m.fs.treatment.costing.aggregate_flow_electricity):<10.1f}{"kW"}'
    )
    print(
        f'{"PV Agg. Flow Elec.":<30s}{value(m.fs.energy.costing.aggregate_flow_electricity):<10.1f}{"kW"}'
    )
    print(
        f'{"Electricity Buy":<30s}{f"{value(m.fs.costing.aggregate_flow_electricity_purchased):<10,.0f}"}{"kW":<10s}'
    )
    print(
        f'{"Electricity Sold":<30s}{f"{value(m.fs.costing.aggregate_flow_electricity_sold):<10,.0f}"}{"kW":<10s}'
    )
    print(
        f'{"Electricity Cost":<29s}{f"${value(m.fs.costing.total_electric_operating_cost):<10,.0f}"}{"$/yr":<10s}'
    )

    print(m.fs.energy.pv.annual_energy.display())
    print(m.fs.energy.pv.costing.annual_generation.display())
    print(m.fs.costing.total_electric_operating_cost.display())

def breakdown_dof(blk):
    equalities = [c for c in activated_equalities_generator(blk)]
    active_vars = variables_in_activated_equalities_set(blk)
    fixed_active_vars = fixed_variables_in_activated_equalities_set(blk)
    unfixed_active_vars = unfixed_variables_in_activated_equalities_set(blk)
    print("\n ===============DOF Breakdown================\n")
    print(f'Degrees of Freedom: {degrees_of_freedom(blk)}')
    print(f"Activated Variables: ({len(active_vars)})")
    for v in active_vars:
        print(f"   {v}")
    print(f"Activated Equalities: ({len(equalities)})")
    for c in equalities:
        print(f"   {c}")

    print(f'Fixed Active Vars: ({len(fixed_active_vars)})')
    for v in fixed_active_vars:
        print(f'   {v}')

    print(f'Unfixed Active Vars: ({len(unfixed_active_vars)})')
    for v in unfixed_active_vars:
        print(f'   {v}')
    print('\n')
    print(f" {f' Active Vars':<30s}{len(active_vars)}")
    print(f"{'-'}{f' Fixed Active Vars':<30s}{len(fixed_active_vars)}")
    print(f"{'-'}{f' Activated Equalities':<30s}{len(equalities)}")
    print(f"{'='}{f' Degrees of Freedom':<30s}{degrees_of_freedom(blk)}")
    print('\nSuggested Variables to Fix:')

    if degrees_of_freedom != 0:
        unfixed_vars_without_constraint = [v for v in active_vars if v not in unfixed_active_vars]
        for v in unfixed_vars_without_constraint:
            if v.fixed is False:
                print(f'   {v}')


if __name__ == "__main__":
    m = build_system()
    build_pv(m)
    set_pv_constraints(m, focus="Size")
    add_pv_costing(m, m.fs.energy.pv)

    