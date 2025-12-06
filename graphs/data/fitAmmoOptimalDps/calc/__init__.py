# =============================================================================
# Copyright (C) 2010 Diego Duclos
#
# This file is part of pyfa.
#
# pyfa is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# pyfa is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with pyfa.  If not, see <http://www.gnu.org/licenses/>.
# =============================================================================

"""
Calculation modules for optimal ammo selection.

This package contains the modular calculation functions for the
fitAmmoOptimalDps graph, split from the original monolithic getter.py.

Modules:
    turret: Turret mechanics (tracking, angular speed, applied damage)
    charges: Charge filtering, stats, and precomputation
    optimize_ammo: Ammo optimization (pareto filter, transitions)
    projected: Distance-keyed cache for target speed/sig from projected effects
    launcher: Missile mechanics (stub - to be implemented)
"""

# Import key functions for convenient access
from .projected import (
    buildProjectedCache,
    getProjectedParamsAtDistance,
)
