# coding: utf-8
# Copyright (c) Materials Virtual Lab.
# Distributed under the terms of the BSD License.

from __future__ import division, unicode_literals, print_function

from pymatgen.core import Structure, PeriodicSite
from pymatgen.core.periodic_table import get_el_sp
from pymatgen.symmetry.analyzer import SpacegroupAnalyzer

import warnings
import numpy as np
import itertools

__author__ = "Iek-Heng Chu"
__version__ = "1.0"
__date__ = "March 14, 2017"

"""
Algorithms for NEB migration path analysis.
"""


# TODO: (1) ipython notebook example files, unittests


class IDPPSolver(object):
    """
    A solver using image dependent pair potential (IDPP) algo to get an improved
    initial NEB path. For more details about this algo, please refer to 
    Smidstrup et al., J. Chem. Phys. 140, 214106 (2014).

    """

    def __init__(self, structures):
        """
        Initialization.

        Args:
            structures (list of pmg_structure) : Initial guess of the NEB path 
                (including initial and final end-point structures).
        """

        latt = structures[0].lattice
        natoms = structures[0].num_sites
        nimages = len(structures) - 2
        target_dists = []

        # Initial guess of the path (in Cartesian coordinates) used in the IDPP
        # algo.
        init_coords = []

        # Construct the set of target distance matrices via linear interpolation
        # between those of end-point structures.
        for i in range(1, nimages + 1):
            # Interpolated distance matrices
            dist = structures[0].distance_matrix + i / (nimages + 1) * (
                structures[-1].distance_matrix - structures[0].distance_matrix)

            target_dists.append(dist)

        target_dists = np.array(target_dists)

        # Set of translational vector matrices (anti-symmetric) for the images.
        translations = np.zeros((nimages, natoms, natoms, 3), dtype=np.float64)

        # A set of weight functions. It is set as 1/d^4 for each image. Here,
        # we take d as the average of the target distance matrix and the actual
        # distance matrix.
        weights = np.zeros_like(target_dists, dtype=np.float64)
        for ni in range(nimages):
            avg_dist = (target_dists[ni]
                        + structures[ni + 1].distance_matrix) / 2.0
            weights[ni] = 1.0 / (avg_dist ** 4 +
                                 np.eye(natoms, dtype=np.float64) * 1e-8)

        for ni, i in itertools.product(range(nimages + 2), range(natoms)):
            frac_coords = structures[ni][i].frac_coords
            init_coords.append(latt.get_cartesian_coords(frac_coords))

            if ni not in [0, nimages + 1]:
                for j in range(i + 1, natoms):
                    img = latt.get_distance_and_image(
                        frac_coords, structures[ni][j].frac_coords)[1]
                    translations[ni - 1, i, j] = latt.get_cartesian_coords(img)
                    translations[ni - 1, j, i] = -latt.get_cartesian_coords(img)

        self.init_coords = np.array(init_coords).reshape(nimages + 2, natoms, 3)
        self.translations = translations
        self.weights = weights
        self.structures = structures
        self.target_dists = target_dists
        self.nimages = nimages

    def run(self, maxiter=1000, tol=1e-5, gtol=1e-3, step_size=0.05,
            max_disp=0.05, spring_const=5.0, species=None):
        """
        Perform iterative minimization of the set of objective functions in an 
        NEB-like manner. In each iteration, the total force matrix for each 
        image is constructed, which comprises both the spring forces and true 
        forces. For more details about the NEB approach, please see the 
        references, e.g. Henkelman et al., J. Chem. Phys. 113, 9901 (2000).

        Args:
            maxiter (int): Maximum number of iterations in the minimization 
                process.
            tol (float): Tolerance of the change of objective functions between
                consecutive steps.
            gtol (float): Tolerance of maximum force component (absolute value).
            step_size (float): Step size associated with the displacement of 
                the atoms during the minimization process.
            max_disp (float): Maximum allowed atomic displacement in each 
                iteration.
            spring_const (float): A virtual spring constant used in the NEB-like
                        relaxation process that yields so-called IDPP path.
            species (list of string): If provided, only those given species are 
                allowed to move. The atomic positions of other species are
                obtained via regular linear interpolation approach.

        Returns:
            [Structure] Complete IDPP path (including end-point structures)
        """

        n = 0
        coords = self.init_coords.copy()
        old_funcs = np.zeros((self.nimages,), dtype=np.float64)
        idpp_structures = [self.structures[0]]

        if species is None:
            indices = list(range(len(self.structures[0])))
        else:
            species = [get_el_sp(sp) for sp in species]
            indices = [i for i, site in enumerate(self.structures[0])
                       if site.specie in species]

            if len(indices) == 0:
                raise ValueError("The given species are not in the system!")

        # Iterative minimization
        for n in range(maxiter):
            # Get the sets of objective functions, true and total force
            # matrices.
            funcs, true_forces = self._get_funcs_and_forces(coords)
            tot_forces = self._get_total_forces(coords, true_forces,
                                                spring_const=spring_const)

            # Each atom is allowed to move up to max_disp
            disp_mat = step_size * tot_forces[:, indices, :]
            disp_mat = np.where(np.abs(disp_mat) > max_disp,
                                np.sign(disp_mat) * max_disp,
                                disp_mat)
            coords[1:(self.nimages + 1), indices] += disp_mat

            max_force = np.abs(tot_forces[:, indices, :]).max()
            tot_res = np.sum(np.abs(old_funcs - funcs))

            if tot_res < tol and max_force < gtol:
                break

            old_funcs = funcs

        else:
            warnings.warn(
                "Maximum iteration number is reached without convergence!",
                UserWarning)

        for ni in range(self.nimages):
            # generate the improved image structure
            new_sites = []

            for site, cart_coords in zip(self.structures[ni + 1], coords[ni + 1]):
                new_site = PeriodicSite(
                    site.species_and_occu, coords=cart_coords,
                    lattice=site.lattice, coords_are_cartesian=True,
                    properties=site.properties)
                new_sites.append(new_site)

            idpp_structures.append(Structure.from_sites(new_sites))

        # Also include end-point structure.
        idpp_structures.append(self.structures[-1])

        return idpp_structures

    @classmethod
    def from_endpoints(cls, endpoints, nimages=5, sort_tol=1.0):
        """
        A class method that starts with end-point structures instead. The 
        initial guess for the IDPP algo is then constructed using linear
        interpolation.

        Args:
            endpoints (list of Structure objects): The two end-point structures.
            nimages (int): Number of images between the two end-points.
            sort_tol (float): Distance tolerance (in Angstrom) used to match the
                atomic indices between start and end structures. Need
                to increase the value in some cases.
        """
        try:
            images = endpoints[0].interpolate(endpoints[1], nimages=nimages + 1,
                                              autosort_tol=sort_tol)
        except Exception as e:
            if "Unable to reliably match structures " in str(e):
                warnings.warn("Auto sorting is turned off because it is unable"
                              " to match the end-point structures!",
                              UserWarning)
                images = endpoints[0].interpolate(endpoints[1],
                                                  nimages=nimages + 1,
                                                  autosort_tol=0)
            else:
                raise e

        return IDPPSolver(images)

    def _get_funcs_and_forces(self, x):
        """
        Calculate the set of objective functions as well as their gradients, 
        i.e. "effective true forces"
        """
        funcs = []
        funcs_prime = []
        natoms = np.shape(self.translations)[1]

        for ni in range(len(x) - 2):
            vec = np.array([x[ni + 1, i] - x[ni + 1] -
                            self.translations[ni, i] for i in range(natoms)])
            trial_dist = np.linalg.norm(vec, axis=2)
            aux = -2 * (trial_dist - self.target_dists[ni]) * self.weights[ni] \
                / (trial_dist + np.eye(natoms, dtype=np.float64))

            # Objective function
            func = 0.5 * np.sum((trial_dist -
                                 self.target_dists[ni]) ** 2 * self.weights[ni])

            # "True force" derived from the objective function.
            grad = np.zeros_like(x[0], dtype=np.float64)

            for i in range(natoms):
                grad[i] = np.dot(aux[i], vec[i])

            funcs.append(func)
            funcs_prime.append(grad)

        return np.array(funcs), np.array(funcs_prime)

    @staticmethod
    def get_unit_vector(vec):
        return vec / np.linalg.norm(vec)

    def _get_total_forces(self, x, true_forces, spring_const):
        """
        Calculate the total force on each image structure, which is equal to 
        the spring force along the tangent + true force perpendicular to the 
        tangent. Note that the spring force is the modified version in the
        literature (e.g. Henkelman et al., J. Chem. Phys. 113, 9901 (2000)).
        """

        total_forces = []
        natoms = np.shape(true_forces)[1]

        for ni in range(1, len(x) - 1):
            vec1 = (x[ni + 1] - x[ni]).flatten()
            vec2 = (x[ni] - x[ni - 1]).flatten()

            # Local tangent
            tangent = self.get_unit_vector(vec1) + self.get_unit_vector(vec2)
            tangent = self.get_unit_vector(tangent)

            # Spring force
            spring_force = spring_const * (np.linalg.norm(vec1) -
                                           np.linalg.norm(vec2)) * tangent

            # Total force
            flat_ft = true_forces[ni - 1].copy().flatten()
            total_force = true_forces[ni - 1] + (
                spring_force - np.dot(flat_ft, tangent) * tangent).reshape(
                natoms, 3)
            total_forces.append(total_force)

        return np.array(total_forces)


class MigrationPath(object):
    """
    A convenience container representing a migration path.
    """

    def __init__(self, isite, esite, symm_structure):
        """
        Args:
            isite: Initial site
            esite: End site
            symm_structure: SymmetrizedStructure
        """
        self.isite = isite
        self.esite = esite
        self.symm_structure = symm_structure
        self.msite = PeriodicSite(
            esite.specie,
            (isite.frac_coords + esite.frac_coords) / 2, esite.lattice)
        sg = self.symm_structure.spacegroup
        for i, sites in enumerate(self.symm_structure.equivalent_sites):
            if sg.are_symmetrically_equivalent([isite], [sites[0]]):
                self.iindex = i
            if sg.are_symmetrically_equivalent([esite], [sites[0]]):
                self.eindex = i

    def __repr__(self):
        return "Path of %.4f A from %s %s (index: %d) to %s %s (index: %d)" \
            % (self.length, self.isite.specie, self.isite.frac_coords,
               self.iindex, self.esite.specie, self.esite.frac_coords,
               self.eindex)

    @property
    def length(self):
        """
        Returns:
            (float) Length of migration path.
        """
        return np.linalg.norm(self.isite.coords - self.esite.coords)

    def __hash__(self):
        return self.iindex + self.eindex

    def __str__(self):
        return self.__repr__()

    def __eq__(self, other):
        if self.symm_structure != other.symm_structure:
            return False

        return self.symm_structure.spacegroup.are_symmetrically_equivalent(
            (self.isite, self.msite, self.esite),
            (other.isite, other.msite, other.esite)
        )

    def get_structures(self, nimages=5, vac_mode=True, idpp=False,
                       **idpp_kwargs):
        """
        Generate structures for NEB calculation.
        
        Args:
            nimages (int): Defaults to 5. Number of NEB images. Total number of
                structures returned in nimages+2.
            vac_mode (bool): Defaults to True. In vac_mode, a vacancy diffusion
                mechanism is assumed. The initial and end sites of the path
                are assumed to be the initial and ending positions of the
                vacancies. If vac_mode is False, an interstitial mechanism is
                assumed. The initial and ending positions are assumed to be
                the initial and ending positions of the interstitial, and all
                other sites of the same specie are removed. E.g., if NEBPaths
                were obtained using a Li4Fe4P4O16 structure, vac_mode=True would
                generate structures with formula Li3Fe4P4O16, while 
                vac_mode=False would generate structures with formula 
                LiFe4P4O16.
            idpp (bool): Defaults to False. If True, the generated structures
                will be run through the IDPPSolver to generate a better guess
                for the minimum energy path.
            \*\*idpp_kwargs: Passthrough kwargs for the IDPPSolver.run.

        Returns:
            [Structure] Note that the first site of each structure is always
            the migrating ion. This makes it easier to perform subsequent
            analysis.
        """
        migrating_specie_sites = []
        other_sites = []
        isite = self.isite
        esite = self.esite

        for site in self.symm_structure.sites:
            if site.specie != isite.specie:
                other_sites.append(site)
            else:
                if vac_mode and (isite.distance(site) > 1e-8 and
                                 esite.distance(site) > 1e-8):
                    migrating_specie_sites.append(site)

        start_structure = Structure.from_sites(
            [self.isite] + migrating_specie_sites + other_sites)
        end_structure = Structure.from_sites(
            [self.esite] + migrating_specie_sites + other_sites)

        structures = start_structure.interpolate(end_structure,
                                                 nimages=nimages + 1,
                                                 pbc=False)

        if idpp:
            solver = IDPPSolver(structures)
            return solver.run(**idpp_kwargs)

        return structures

    def write_path(self, fname, **kwargs):
        """
        Write the path to a file for easy viewing.
        
        Args:
            fname (str): File name. 
            \*\*kwargs: Kwargs supported by NEBPath.get_structures.
        """
        sites = []
        for st in self.get_structures(**kwargs):
            sites.extend(st)
        st = Structure.from_sites(sites)
        st.to(filename=fname)


class DistinctPathFinder(object):
    """
    Determines symmetrically distinct paths between existing sites.
    The path info can then be used to set up either vacancy or interstitial
    diffusion (assuming site positions are known). Note that this works mainly
    for atomic mechanism, and does not work for correlated migration.
    """

    def __init__(self, structure, migrating_specie, max_path_length=5,
                 symprec=0.1):
        """
        Args:
            structure: Input structure that contains all sites.
            migrating_specie (Specie-like): The specie that migrates. E.g., 
                "Li".
            max_path_length (float): Maximum length of NEB path. Defaults to 5
                Angstrom. Usually, you'd want to set this close to the longest
                lattice parameter / diagonal in the cell to ensure all paths
                are found.
            symprec (float): Symmetry precision to determine equivalence. 
        """
        self.structure = structure
        self.migrating_specie = get_el_sp(migrating_specie)
        self.max_path_length = max_path_length
        self.symprec = symprec
        a = SpacegroupAnalyzer(self.structure, symprec=self.symprec)
        self.symm_structure = a.get_symmetrized_structure()

    def get_paths(self):
        """
        Returns:
            [MigrationPath] All distinct migration paths.
        """
        paths = set()
        for sites in self.symm_structure.equivalent_sites:
            if sites[0].specie == self.migrating_specie:
                site0 = sites[0]
                for nn, dist in self.symm_structure.get_neighbors(
                        site0, r=self.max_path_length):
                    if nn.specie == self.migrating_specie:
                        path = MigrationPath(site0, nn, self.symm_structure)
                        paths.add(path)

        return sorted(paths, key=lambda p: p.length)

    def write_all_paths(self, fname, nimages=5, **kwargs):
        """
        Write a file containing all paths, using hydrogen as a placeholder for
        the images. H is chosen as it is the smallest atom. This is extremely
        useful for path visualization in a standard software like VESTA.
        
        Args:
            fname (str): Filename 
            nimages (int): Number of images per path.
            \*\*kwargs: Passthrough kwargs to path.get_structures.
        """
        sites = []
        for p in self.get_paths():
            structures = p.get_structures(
                nimages=nimages, species=[self.migrating_specie], **kwargs)
            sites.append(structures[0][0])
            sites.append(structures[-1][0])
            for s in structures[1:-1]:
                sites.append(PeriodicSite("H", s[0].frac_coords, s.lattice))
        sites.extend(structures[0].sites[1:])
        Structure.from_sites(sites).to(filename=fname)