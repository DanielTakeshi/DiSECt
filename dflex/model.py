# Copyright (c) 2021 NVIDIA CORPORATION.  All rights reserved.
# NVIDIA CORPORATION and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.  Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA CORPORATION is strictly prohibited.

"""A module for building simulation models and state.
"""

import math
import torch
import numpy as np
from copy import copy

from typing import Tuple
from typing import List
Vec3 = List[float]
Vec4 = List[float]
Quat = List[float]
Mat33 = List[float]
Transform = Tuple[Vec3, Quat]

from dflex.util import *

# shape geometry types
GEO_SPHERE = 0
GEO_BOX = 1
GEO_CAPSULE = 2
GEO_MESH = 3
GEO_SDF = 4
GEO_PLANE = 5
GEO_NONE = 6

# body joint types
JOINT_PRISMATIC = 0 
JOINT_REVOLUTE = 1
JOINT_BALL = 2
JOINT_FIXED = 3
JOINT_FREE = 4

class Mesh:
    """Describes a triangle collision mesh for simulation

    Attributes:

        vertices (List[Vec3]): Mesh vertices
        indices (List[int]): Mesh indices
        I (Mat33): Inertia tensor of the mesh assuming density of 1.0 (around the center of mass)
        mass (float): The total mass of the body assuming density of 1.0
        com (Vec3): The center of mass of the body
    """

    def __init__(self, vertices: List[Vec3], indices: List[int]):
        """Construct a Mesh object from a triangle mesh

        The mesh center of mass and inertia tensor will automatically be 
        calculated using a density of 1.0. This computation is only valid
        if the mesh is closed (two-manifold).

        Args:
            vertices: List of vertices in the mesh
            indices: List of triangle indices, 3 per-element       
        """

        self.vertices = vertices
        self.indices = indices

        # compute com and inertia (using density=1.0)
        com = np.mean(vertices, 0)

        num_tris = int(len(indices) / 3)

        # compute signed inertia for each tetrahedron
        # formed with the interior point, using an order-2
        # quadrature: https://www.sciencedirect.com/science/article/pii/S0377042712001604#br000040

        weight = 0.25
        alpha = math.sqrt(5.0) / 5.0

        I = np.zeros((3, 3))
        mass = 0.0

        for i in range(num_tris):

            p = np.array(vertices[indices[i * 3 + 0]])
            q = np.array(vertices[indices[i * 3 + 1]])
            r = np.array(vertices[indices[i * 3 + 2]])

            mid = (com + p + q + r) / 4.0

            pcom = p - com
            qcom = q - com
            rcom = r - com

            Dm = np.matrix((pcom, qcom, rcom)).T
            volume = np.linalg.det(Dm) / 6.0

            # quadrature points lie on the line between the
            # centroid and each vertex of the tetrahedron
            quads = (mid + (p - mid) * alpha, mid + (q - mid) * alpha, mid + (r - mid) * alpha, mid + (com - mid) * alpha)

            for j in range(4):

                # displacement of quadrature point from COM
                d = quads[j] - com

                I += weight * volume * (length_sq(d) * np.eye(3, 3) - np.outer(d, d))
                mass += weight * volume

        self.I = I
        self.mass = mass
        self.com = com


class State:
    """The State object holds all *time-varying* data for a model.
    
    Time-varying data includes particle positions, velocities, rigid body states, and
    anything that is output from the integrator as derived data, e.g.: forces. 
    
    The exact attributes depend on the contents of the model. State objects should
    generally be created using the :func:`Model.state()` function.

    Attributes:

        particle_q (torch.Tensor): Tensor of particle positions
        particle_qd (torch.Tensor): Tensor of particle velocities

        joint_q (torch.Tensor): Tensor of joint coordinates
        joint_qd (torch.Tensor): Tensor of joint velocities
        joint_act (torch.Tensor): Tensor of joint actuation values

    """

    def __init__(self):
        
        self.particle_count = 0
        self.link_count = 0


    def flatten(self):
        """Returns a list of Tensors stored by the state

        This function is intended to be used internal-only but can be used to obtain
        a set of all tensors owned by the state.
        """

        tensors = []

        # build a list of all tensor attributes
        for attr, value in self.__dict__.items():
            if (torch.is_tensor(value)):
                tensors.append(value)

        return tensors


class Model:
    """Holds the definition of the simulation model

    This class holds the non-time varying description of the system, i.e.:
    all geometry, constraints, and parameters used to describe the simulation.

    Attributes:
        particle_q (torch.Tensor): Particle positions, shape [particle_count, 3], float
        particle_qd (torch.Tensor): Particle velocities, shape [particle_count, 3], float
        particle_mass (torch.Tensor): Particle mass, shape [particle_count], float
        particle_inv_mass (torch.Tensor): Particle inverse mass, shape [particle_count], float

        shape_transform (torch.Tensor): Rigid shape transforms, shape [shape_count, 7], float
        shape_body (torch.Tensor): Rigid shape body index, shape [shape_count], int
        shape_geo_type (torch.Tensor): Rigid shape geometry type, [shape_count], int
        shape_geo_src (torch.Tensor): Rigid shape geometry source, shape [shape_count], int
        shape_geo_scale (torch.Tensor): Rigid shape geometry scale, shape [shape_count, 3], float
        shape_materials (torch.Tensor): Rigid shape contact materials, shape [shape_count, 4], float

        spring_indices (torch.Tensor): Particle spring indices, shape [spring_count*2], int
        spring_rest_length (torch.Tensor): Particle spring rest length, shape [spring_count], float
        spring_stiffness (torch.Tensor): Particle spring stiffness, shape [spring_count], float
        spring_damping (torch.Tensor): Particle spring damping, shape [spring_count], float
        spring_control (torch.Tensor): Particle spring activation, shape [spring_count], float

        tri_indices (torch.Tensor): Triangle element indices, shape [tri_count*3], int
        tri_poses (torch.Tensor): Triangle element rest pose, shape [tri_count, 2, 2], float
        tri_activations (torch.Tensor): Triangle element activations, shape [tri_count], float

        edge_indices (torch.Tensor): Bending edge indices, shape [edge_count*2], int
        edge_rest_angle (torch.Tensor): Bending edge rest angle, shape [edge_count], float

        tet_indices (torch.Tensor): Tetrahedral element indices, shape [tet_count*4], int
        tet_poses (torch.Tensor): Tetrahedral rest poses, shape [tet_count, 3, 3], float
        tet_activations (torch.Tensor): Tetrahedral volumetric activations, shape [tet_count], float
        tet_mu (torch.Tensor): Tetrahedral elastic parameter, shape [tet_count], float
        tet_lambda (torch.Tensor): Tetrahedral elastic parameter, shape [tet_count], float
        tet_damping (torch.Tensor): Tetrahedral elastic parameter, shape [tet_count], float
        
        body_X_cm (torch.Tensor): Rigid body center of mass (in local frame), shape [link_count, 7], float
        body_I_m (torch.Tensor): Rigid body inertia tensor (relative to COM), shape [link_count, 3, 3], float

        articulation_start (torch.Tensor): Articulation start offset, shape [num_articulations], int

        joint_q (torch.Tensor): Joint coordinate, shape [joint_coord_count], float
        joint_qd (torch.Tensor): Joint velocity, shape [joint_dof_count], float
        joint_type (torch.Tensor): Joint type, shape [joint_count], int
        joint_parent (torch.Tensor): Joint parent, shape [joint_count], int
        joint_X_pj (torch.Tensor): Joint transform in parent frame, shape [joint_count, 7], float
        joint_X_cm (torch.Tensor): Joint mass frame in child frame, shape [joint_count, 7], float
        joint_axis (torch.Tensor): Joint axis in child frame, shape [joint_count, 3], float
        joint_q_start (torch.Tensor): Joint coordinate offset, shape [joint_count], int
        joint_qd_start (torch.Tensor): Joint velocity offset, shape [joint_count], int

        joint_armature (torch.Tensor): Armature for each joint, shape [joint_count], float
        joint_target_ke (torch.Tensor): Joint stiffness, shape [joint_count], float
        joint_target_kd (torch.Tensor): Joint damping, shape [joint_count], float
        joint_target (torch.Tensor): Joint target, shape [joint_count], float

        particle_count (int): Total number of particles in the system
        joint_coord_count (int): Total number of joint coordinates in the system
        joint_dof_count (int): Total number of joint dofs in the system
        link_count (int): Total number of links in the system
        shape_count (int): Total number of shapes in the system
        tri_count (int): Total number of triangles in the system
        tet_count (int): Total number of tetrahedra in the system
        edge_count (int): Total number of edges in the system
        spring_count (int): Total number of springs in the system
        contact_count (int): Total number of contacts in the system
        
    Note:
        It is strongly recommended to use the ModelBuilder to construct a simulation rather
        than creating your own Model object directly, however it is possible to do so if 
        desired.
    """

    def __init__(self, adapter):

        self.particle_q = None
        self.particle_qd = None
        self.particle_mass = None
        self.particle_inv_mass = None

        self.shape_transform = None
        self.shape_body = None
        self.shape_geo_type = None
        self.shape_geo_src = None
        self.shape_geo_scale = None
        self.shape_materials = None

        self.spring_indices = None
        self.spring_rest_length = None
        self.spring_stiffness = None
        self.spring_damping = None
        self.spring_control = None

        self.tri_indices = None
        self.tri_poses = None
        self.tri_activations = None

        self.edge_indices = None
        self.edge_rest_angle = None

        self.tet_indices = None
        self.tet_poses = None
        self.tet_activations = None
        self.tet_mu = None
        self.tet_lambda = None
        self.tet_damping = None
        
        self.body_X_cm = None
        self.body_I_m = None

        self.articulation_start = None

        self.joint_q = None
        self.joint_qd = None
        self.joint_type = None
        self.joint_parent = None
        self.joint_X_pj = None
        self.joint_X_cm = None
        self.joint_axis = None
        self.joint_q_start = None
        self.joint_qd_start = None

        self.joint_armature = None
        self.joint_target_ke = None
        self.joint_target_kd = None
        self.joint_target = None

        self.particle_count = 0
        self.joint_coord_count = 0
        self.joint_dof_count = 0
        self.link_count = 0
        self.shape_count = 0
        self.tri_count = 0
        self.tet_count = 0
        self.edge_count = 0
        self.spring_count = 0
        self.contact_count = 0

        self.gravity = torch.tensor((0.0, -9.80665, 0.0), dtype=torch.float32, device=adapter)

        self.contact_distance = 1e-5
        self.contact_ke = 1.e+3
        self.contact_kd = 0.0
        self.contact_kf = 1.e+3
        self.contact_mu = 0.5

        self.tri_ke = 100.0
        self.tri_ka = 100.0
        self.tri_kd = 10.0
        self.tri_kb = 100.0
        self.tri_drag = 0.0
        self.tri_lift = 0.0

        self.edge_ke = 100.0
        self.edge_kd = 0.0

        self.particle_radius = 1e-4
        self.adapter = adapter

    def state(self) -> State:
        """Returns a state object for the model

        The returned state will be initialized with the initial configuration given in
        the model description.
        """

        s = State()

        s.particle_count = self.particle_count
        s.link_count = self.link_count

        #--------------------------------
        # dynamic state (input, output)
          
        # particles
        if (self.particle_count):
            s.particle_q = torch.clone(self.particle_q)
            s.particle_qd = torch.clone(self.particle_qd)

        # articulations
        if (self.link_count):
            s.joint_q = torch.clone(self.joint_q)
            s.joint_qd = torch.clone(self.joint_qd)
            s.joint_act = torch.zeros_like(self.joint_qd)

        #--------------------------------
        # derived state (output only)
        
        if (self.particle_count):
            s.particle_f = torch.empty_like(self.particle_qd, requires_grad=True)


        if (self.link_count):
            
            # joints
            s.joint_qdd = torch.zeros_like(self.joint_qd, requires_grad=True)
            s.joint_tau = torch.zeros_like(self.joint_qd, requires_grad=True)
            s.joint_S_s = torch.empty((self.joint_dof_count, 6), dtype=torch.float32, device=self.adapter, requires_grad=True)            

            # derived rigid body data (maximal coordinates)
            s.body_X_sc = torch.empty((self.link_count, 7), dtype=torch.float32, device=self.adapter, requires_grad=True)
            s.body_X_sm = torch.empty((self.link_count, 7), dtype=torch.float32, device=self.adapter, requires_grad=True)
            s.body_I_s = torch.empty((self.link_count, 6, 6), dtype=torch.float32, device=self.adapter, requires_grad=True)
            s.body_v_s = torch.empty((self.link_count, 6), dtype=torch.float32, device=self.adapter, requires_grad=True)
            s.body_a_s = torch.empty((self.link_count, 6), dtype=torch.float32, device=self.adapter, requires_grad=True)
            s.body_f_s = torch.zeros((self.link_count, 6), dtype=torch.float32, device=self.adapter, requires_grad=True)
            #s.body_ft_s = torch.zeros((self.link_count, 6), dtype=torch.float32, device=self.adapter, requires_grad=False)
            #s.body_f_ext_s = torch.zeros((self.link_count, 6), dtype=torch.float32, device=self.adapter, requires_grad=False)

        if (self.cut_spring_count):
            # stores 3D knife force for both sides of each cutting spring
            s.knife_f = torch.zeros((self.cut_spring_count * 2, 3), dtype=torch.float32, device=self.adapter)
            s.cut_spring_ke = torch.clone(self.cut_spring_stiffness)
            s.cut_spring_kd = torch.clone(self.cut_spring_damping)

        return s

    def alloc_mass_matrix(self):

        if (self.link_count):

            # system matrices
            self.M = torch.zeros(self.M_size, dtype=torch.float32, device=self.adapter, requires_grad=False)
            self.J = torch.zeros(self.J_size, dtype=torch.float32, device=self.adapter, requires_grad=False)
            self.P = torch.empty(self.J_size, dtype=torch.float32, device=self.adapter, requires_grad=False)
            self.H = torch.empty(self.H_size, dtype=torch.float32, device=self.adapter, requires_grad=False)

            # zero since only upper triangle is set which can trigger NaN detection
            self.L = torch.zeros(self.H_size, dtype=torch.float32, device=self.adapter, requires_grad=False)

    def flatten(self):
        """Returns a list of Tensors stored by the model

        This function is intended to be used internal-only but can be used to obtain
        a set of all tensors owned by the model.
        """

        tensors = []

        # build a list of all tensor attributes
        for attr, value in self.__dict__.items():
            if (torch.is_tensor(value)):
                tensors.append(value)

        return tensors

    # builds contacts
    def collide(self, state: State):
        """Constructs a set of contacts between rigid bodies and ground

        This method performs collision detection between rigid body vertices in the scene and updates
        the model's set of contacts stored as the following attributes:

            * **contact_body0**: Tensor of ints with first rigid body index 
            * **contact_body1**: Tensor of ints with second rigid body index (currently always -1 to indicate ground)
            * **contact_point0**: Tensor of Vec3 representing contact point in local frame of body0
            * **contact_dist**: Tensor of float values representing the distance to maintain
            * **contact_material**: Tensor contact material indices

        Args:
            state: The state of the simulation at which to perform collision detection

        Note:
            Currently this method uses an 'all pairs' approach to contact generation that is
            state indepdendent. In the future this will change and will create a node in
            the computational graph to propagate gradients as a function of state.

        Todo:

            Only ground-plane collision is currently implemented. Since the ground is static
            it is acceptable to call this method once at initialization time.
        """

        body0 = []
        body1 = []
        point = []
        dist = []
        mat = []

        def add_contact(b0, b1, t, p0, d, m):
            body0.append(b0)
            body1.append(b1)
            point.append(transform_point(t, np.array(p0)))
            dist.append(d)
            mat.append(m)

        for i in range(self.shape_count):

            # transform from shape to body
            X_bs = transform_expand(self.shape_transform[i].tolist())

            geo_type = self.shape_geo_type[i].item()

            if (geo_type == GEO_SPHERE):

                radius = self.shape_geo_scale[i][0].item()

                add_contact(self.shape_body[i], -1, X_bs, (0.0, 0.0, 0.0), radius, i)

            elif (geo_type == GEO_CAPSULE):

                radius = self.shape_geo_scale[i][0].item()
                half_width = self.shape_geo_scale[i][1].item()

                add_contact(self.shape_body[i], -1, X_bs, (-half_width, 0.0, 0.0), radius, i)
                add_contact(self.shape_body[i], -1, X_bs, (half_width, 0.0, 0.0), radius, i)

            elif (geo_type == GEO_BOX):

                edges = self.shape_geo_scale[i].tolist()

                add_contact(self.shape_body[i], -1, X_bs, (-edges[0], -edges[1], -edges[2]), 0.0, i)        
                add_contact(self.shape_body[i], -1, X_bs, ( edges[0], -edges[1], -edges[2]), 0.0, i)
                add_contact(self.shape_body[i], -1, X_bs, (-edges[0],  edges[1], -edges[2]), 0.0, i)
                add_contact(self.shape_body[i], -1, X_bs, (edges[0], edges[1], -edges[2]), 0.0, i)
                add_contact(self.shape_body[i], -1, X_bs, (-edges[0], -edges[1], edges[2]), 0.0, i)
                add_contact(self.shape_body[i], -1, X_bs, (edges[0], -edges[1], edges[2]), 0.0, i)
                add_contact(self.shape_body[i], -1, X_bs, (-edges[0], edges[1], edges[2]), 0.0, i)
                add_contact(self.shape_body[i], -1, X_bs, (edges[0], edges[1], edges[2]), 0.0, i)

            elif (geo_type == GEO_MESH):

                mesh = self.shape_geo_src[i]
                scale = self.shape_geo_scale[i].detach().cpu().numpy()

                for v in mesh.vertices:

                    p = (v[0] * scale[0], v[1] * scale[1], v[2] * scale[2])

                    add_contact(self.shape_body[i], -1, X_bs, p, 0.0, i)

        # send to torch
        self.contact_body0 = torch.tensor(body0, dtype=torch.int32, device=self.adapter)
        self.contact_body1 = torch.tensor(body1, dtype=torch.int32, device=self.adapter)
        self.contact_point0 = torch.tensor(point, dtype=torch.float32, device=self.adapter)
        self.contact_dist = torch.tensor(dist, dtype=torch.float32, device=self.adapter)
        self.contact_material = torch.tensor(mat, dtype=torch.int32, device=self.adapter)

        self.contact_count = len(body0)





class ModelBuilder:
    """A helper class for building simulation models at runtime.

    Use the ModelBuilder to construct a simulation scene. The ModelBuilder
    is independent of PyTorch and builds the scene representation using
    standard Python data structures, this means it is not differentiable. Once :func:`finalize()` 
    has been called the ModelBuilder transfers all data to Torch tensors and returns 
    an object that may be used for simulation.

    Example:

        >>> import dflex as df
        >>>
        >>> builder = df.ModelBuilder()
        >>>
        >>> # anchor point (zero mass)
        >>> builder.add_particle((0, 1.0, 0.0), (0.0, 0.0, 0.0), 0.0)
        >>>
        >>> # build chain
        >>> for i in range(1,10):
        >>>     builder.add_particle((i, 1.0, 0.0), (0.0, 0.0, 0.0), 1.0)
        >>>     builder.add_spring(i-1, i, 1.e+3, 0.0, 0)
        >>>
        >>> # create model
        >>> model = builder.finalize()

    Note:
        It is strongly recommended to use the ModelBuilder to construct a simulation rather
        than creating your own Model object directly, however it is possible to do so if 
        desired.
    """
    
    def __init__(self):

        # particles
        self.particle_q = []
        self.particle_qd = []
        self.particle_mass = []

        # shapes
        self.shape_transform = []
        self.shape_body = []
        self.shape_geo_type = []
        self.shape_geo_scale = []
        self.shape_geo_src = []
        self.shape_materials = []

        # geometry
        self.geo_meshes = []
        self.geo_sdfs = []

        # springs
        self.spring_indices = []
        self.spring_rest_length = []
        self.spring_stiffness = []
        self.spring_damping = []
        self.spring_control = []

        # triangles
        self.tri_indices = []
        self.tri_poses = []
        self.tri_activations = []

        # edges (bending)
        self.edge_indices = []
        self.edge_rest_angle = []

        # tetrahedra
        self.tet_indices = []
        self.tet_poses = []
        self.tet_activations = []
        self.tet_mu = []
        self.tet_lambda = []
        self.tet_damping = []

        # muscles
        self.muscle_start = []
        self.muscle_params = []
        self.muscle_activation = []
        self.muscle_links = []
        self.muscle_points = []

        # rigid bodies
        self.joint_parent = []         # index of the parent body                      (constant)
        self.joint_child = []          # index of the child body                       (constant)
        self.joint_axis = []           # joint axis in child joint frame               (constant)
        self.joint_X_pj = []           # frame of joint in parent                      (constant)
        self.joint_X_cm = []           # frame of child com (in child coordinates)     (constant)

        self.joint_q_start = []        # joint offset in the q array
        self.joint_qd_start = []       # joint offset in the qd array
        self.joint_type = []
        self.joint_armature = []
        self.joint_target_ke = []
        self.joint_target_kd = []
        self.joint_target = []
        self.joint_limit_lower = []
        self.joint_limit_upper = []
        self.joint_limit_ke = []
        self.joint_limit_kd = []

        self.joint_q = []              # generalized coordinates       (input)
        self.joint_qd = []             # generalized velocities        (input)
        self.joint_qdd = []            # generalized accelerations     (id,fd)
        self.joint_tau = []            # generalized actuation         (input)
        self.joint_u = []              # generalized total torque      (fd)

        self.body_mass = []
        self.body_inertia = []
        self.body_com = []

        self.articulation_start = []

        # IDs of particles that are ignored in contact dynamics
        self.contactless_particles = set()
        self.contact_mask = []         # 0 for each inactive particle, 1 otherwise

        # cutting information
        self.cut_edge_indices = []
        self.cut_edge_coords = []
        self.cut_tets = []
        self.cut_tri_indices = []
        self.cut_virtual_tri_indices = []
        self.cut_virtual_tri_indices_above_cut = []
        self.cut_virtual_tri_indices_below_cut = []
        self.sdf_ke = []
        self.sdf_kd = []
        self.sdf_kf = []
        self.sdf_mu = []
        self.sdf_radius = 0.0
        self.cut_spring_indices = []
        self.cut_spring_normal = []
        self.cut_spring_rest_length = []
        self.cut_spring_stiffness = []
        self.cut_spring_damping = []
        self.cut_spring_softness = []
        self.cut_duplicated_x = {}

        self.knife_tri_vertices = []
        self.knife_tri_indices = []

        # coupling springs connecting rigid bodies with particles
        self.coupling_spring_indices = []  # [rigid_body_index, particle_index]
        self.coupling_spring_moment_arm = []
        self.coupling_spring_stiffness = []
        self.coupling_spring_damping = []

        # dependent particles derive their position and velocity from the rigid body
        self.dependent_particle_indices = []  # [rigid_body_index, particle_index]
        self.dependent_particle_moment_arm = []

        # rigid link index of the knife
        self.knife_link_index = 0

    def add_articulation(self) -> int:
        """Add an articulation object, all subsequently added links (see: :func:`add_link`) will belong to this articulation object. 
        Calling this method multiple times 'closes' any previous articulations and begins a new one.

        Returns:
            The index of the articulation
        """
        self.articulation_start.append(len(self.joint_type))
        return len(self.articulation_start)-1


    # rigids, register a rigid body and return its index.
    def add_link(
        self, 
        parent : int, 
        X_pj : Transform, 
        axis : Vec3, 
        type : int, 
        armature: float=0.01, 
        stiffness: float=0.0, 
        damping: float=0.0,
        limit_lower: float=-1.e+3,
        limit_upper: float=1.e+3,
        limit_ke: float=100.0,
        limit_kd: float=10.0,
        com: Vec3=np.zeros(3), 
        I_m: Mat33=np.zeros((3, 3)), 
        m: float=0.0) -> int:
        """Adds a rigid body to the model.

        Args:
            parent: The index of the parent body
            X_pj: The location of the joint in the parent's local frame connecting this body
            axis: The joint axis
            type: The type of joint, should be one of: JOINT_PRISMATIC, JOINT_REVOLUTE, JOINT_BALL, JOINT_FIXED, or JOINT_FREE
            armature: Additional inertia around the joint axis
            stiffness: Spring stiffness that attempts to return joint to zero position
            damping: Spring damping that attempts to remove joint velocity
            com: The center of mass of the body w.r.t its origin
            I_m: The 3x3 inertia tensor of the body (specified relative to the center of mass)
            m: The mass of the body

        Returns:
            The index of the body in the model

        Note:
            If the mass (m) is zero then the body is treated as kinematic with no dynamics

        """

        # joint data
        self.joint_type.append(type)
        self.joint_axis.append(np.array(axis))
        self.joint_parent.append(parent)
        self.joint_X_pj.append(X_pj)

        self.joint_target_ke.append(stiffness)
        self.joint_target_kd.append(damping)
        self.joint_limit_ke.append(limit_ke)
        self.joint_limit_kd.append(limit_kd)        

        self.joint_q_start.append(len(self.joint_q))
        self.joint_qd_start.append(len(self.joint_qd))

        if (type == JOINT_PRISMATIC):
            self.joint_q.append(0.0)
            self.joint_qd.append(0.0)
            self.joint_target.append(0.0)
            self.joint_armature.append(armature)
            self.joint_limit_lower.append(limit_lower)
            self.joint_limit_upper.append(limit_upper)

        elif (type == JOINT_REVOLUTE):
            self.joint_q.append(0.0)
            self.joint_qd.append(0.0)
            self.joint_target.append(0.0)
            self.joint_armature.append(armature)
            self.joint_limit_lower.append(limit_lower)
            self.joint_limit_upper.append(limit_upper)

        elif (type == JOINT_BALL):
            
            # quaternion
            self.joint_q.append(0.0)
            self.joint_q.append(0.0)
            self.joint_q.append(0.0)
            self.joint_q.append(1.0)

            # angular velocity
            self.joint_qd.append(0.0)
            self.joint_qd.append(0.0)
            self.joint_qd.append(0.0)

            # pd targets
            self.joint_target.append(0.0)
            self.joint_target.append(0.0)
            self.joint_target.append(0.0)
            self.joint_target.append(0.0)

            self.joint_armature.append(armature)
            self.joint_armature.append(armature)
            self.joint_armature.append(armature)

            self.joint_limit_lower.append(limit_lower)
            self.joint_limit_lower.append(limit_lower)
            self.joint_limit_lower.append(limit_lower)
            self.joint_limit_lower.append(0.0)
                       
            self.joint_limit_upper.append(limit_upper)
            self.joint_limit_upper.append(limit_upper)
            self.joint_limit_upper.append(limit_upper)
            self.joint_limit_upper.append(0.0)


        elif (type == JOINT_FIXED):
            pass
        elif (type == JOINT_FREE):

            # translation
            self.joint_q.append(0.0)
            self.joint_q.append(0.0)
            self.joint_q.append(0.0)

            # quaternion
            self.joint_q.append(0.0)
            self.joint_q.append(0.0)
            self.joint_q.append(0.0)
            self.joint_q.append(1.0)

            # note armature for free joints should always be zero, better to modify the body inertia directly
            self.joint_armature.append(0.0)
            self.joint_armature.append(0.0)
            self.joint_armature.append(0.0)
            self.joint_armature.append(0.0)
            self.joint_armature.append(0.0)
            self.joint_armature.append(0.0)

            self.joint_target.append(0.0)
            self.joint_target.append(0.0)
            self.joint_target.append(0.0)
            self.joint_target.append(0.0)
            self.joint_target.append(0.0)
            self.joint_target.append(0.0)
            self.joint_target.append(0.0)

            self.joint_limit_lower.append(0.0)
            self.joint_limit_lower.append(0.0)
            self.joint_limit_lower.append(0.0)
            self.joint_limit_lower.append(0.0)
            self.joint_limit_lower.append(0.0)
            self.joint_limit_lower.append(0.0)
            self.joint_limit_lower.append(0.0)
                       
            self.joint_limit_upper.append(0.0)
            self.joint_limit_upper.append(0.0)
            self.joint_limit_upper.append(0.0)
            self.joint_limit_upper.append(0.0)
            self.joint_limit_upper.append(0.0)
            self.joint_limit_upper.append(0.0)
            self.joint_limit_upper.append(0.0)

            # joint velocities
            for i in range(6):
                self.joint_qd.append(0.0)

        self.body_inertia.append(np.zeros((3, 3)))
        self.body_mass.append(0.0)
        self.body_com.append(np.zeros(3))

        # return index of body
        return len(self.joint_type) - 1


    # muscles
    def add_muscle(self, links: List[int], positions: List[Vec3], f0: float, lm: float, lt: float, lmax: float, pen: float) -> float:
        """Adds a muscle-tendon activation unit

        Args:
            links: A list of link indices for each waypoint
            positions: A list of positions of each waypoint in the link's local frame
            f0: Force scaling
            lm: Muscle length
            lt: Tendon length
            lmax: Maximally efficient muscle length

        Returns:
            The index of the muscle in the model

        """

        n = len(links)

        self.muscle_start.append(len(self.muscle_links))
        self.muscle_params.append((f0, lm, lt, lmax, pen))
        self.muscle_activation.append(0.0)

        for i in range(n):

            self.muscle_links.append(links[i])
            self.muscle_points.append(positions[i])

        # return the index of the muscle
        return len(self.muscle_start)-1

    # shapes
    def add_shape_plane(self, plane: Vec4=(0.0, 1.0, 0.0, 0.0), ke: float=1.e+5, kd: float=1000.0, kf: float=1000.0, mu: float=0.5):
        """Adds a plane collision shape

        Args:
            plane: The plane equation in form a*x + b*y + c*z + d = 0
            ke: The contact elastic stiffness
            kd: The contact damping stiffness
            kf: The contact friction stiffness
            mu: The coefficient of friction

        """
        self._add_shape(-1, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), GEO_PLANE, plane, None, 0.0, ke, kd, kf, mu)

    def add_shape_sphere(self, body, pos: Vec3=(0.0, 0.0, 0.0), rot: Quat=(0.0, 0.0, 0.0, 1.0), radius: float=1.0, density: float=1000.0, ke: float=1.e+5, kd: float=1000.0, kf: float=1000.0, mu: float=0.5):
        """Adds a sphere collision shape to a link.

        Args:
            body: The index of the parent link this shape belongs to
            pos: The location of the shape with respect to the parent frame
            rot: The rotation of the shape with respect to the parent frame
            radius: The radius of the sphere
            density: The density of the shape
            ke: The contact elastic stiffness
            kd: The contact damping stiffness
            kf: The contact friction stiffness
            mu: The coefficient of friction

        """

        self._add_shape(body, pos, rot, GEO_SPHERE, (radius, 0.0, 0.0, 0.0), None, density, ke, kd, kf, mu)

    def add_shape_box(self,
                      body : int,
                      pos: Vec3=(0.0, 0.0, 0.0),
                      rot: Quat=(0.0, 0.0, 0.0, 1.0),
                      hx: float=0.5,
                      hy: float=0.5,
                      hz: float=0.5,
                      density: float=1000.0,
                      ke: float=1.e+5,
                      kd: float=1000.0,
                      kf: float=1000.0,
                      mu: float=0.5):
        """Adds a box collision shape to a link.

        Args:
            body: The index of the parent link this shape belongs to
            pos: The location of the shape with respect to the parent frame
            rot: The rotation of the shape with respect to the parent frame
            hx: The half-extents along the x-axis
            hy: The half-extents along the y-axis
            hz: The half-extents along the z-axis
            density: The density of the shape
            ke: The contact elastic stiffness
            kd: The contact damping stiffness
            kf: The contact friction stiffness
            mu: The coefficient of friction

        """

        self._add_shape(body, pos, rot, GEO_BOX, (hx, hy, hz, 0.0), None, density, ke, kd, kf, mu)

    def add_shape_capsule(self,
                          body: int,
                          pos: Vec3=(0.0, 0.0, 0.0),
                          rot: Quat=(0.0, 0.0, 0.0, 1.0),
                          radius: float=1.0,
                          half_width: float=0.5,
                          density: float=1000.0,
                          ke: float=1.e+5,
                          kd: float=1000.0,
                          kf: float=1000.0,
                          mu: float=0.5):
        """Adds a capsule collision shape to a link.

        Args:
            body: The index of the parent link this shape belongs to
            pos: The location of the shape with respect to the parent frame
            rot: The rotation of the shape with respect to the parent frame
            radius: The radius of the capsule
            half_width: The half length of the center cylinder along the x-axis
            density: The density of the shape
            ke: The contact elastic stiffness
            kd: The contact damping stiffness
            kf: The contact friction stiffness
            mu: The coefficient of friction

        """

        self._add_shape(body, pos, rot, GEO_CAPSULE, (radius, half_width, 0.0, 0.0), None, density, ke, kd, kf, mu)

    def add_shape_mesh(self,
                       body: int,
                       pos: Vec3=(0.0, 0.0, 0.0),
                       rot: Quat=(0.0, 0.0, 0.0, 1.0),
                       mesh: Mesh=None,
                       scale: Vec3=(1.0, 1.0, 1.0),
                       density: float=1000.0,
                       ke: float=1.e+5,
                       kd: float=1000.0,
                       kf: float=1000.0,
                       mu: float=0.5):
        """Adds a triangle mesh collision shape to a link.

        Args:
            body: The index of the parent link this shape belongs to
            pos: The location of the shape with respect to the parent frame
            rot: The rotation of the shape with respect to the parent frame
            mesh: The mesh object
            scale: Scale to use for the collider
            density: The density of the shape
            ke: The contact elastic stiffness
            kd: The contact damping stiffness
            kf: The contact friction stiffness
            mu: The coefficient of friction

        """


        self._add_shape(body, pos, rot, GEO_MESH, (scale[0], scale[1], scale[2], 0.0), mesh, density, ke, kd, kf, mu)

    def _add_shape(self, body , pos, rot, type, scale, src, density, ke, kd, kf, mu):
        self.shape_body.append(body)
        self.shape_transform.append(transform(pos, rot))
        self.shape_geo_type.append(type)
        self.shape_geo_scale.append((scale[0], scale[1], scale[2]))
        self.shape_geo_src.append(src)
        self.shape_materials.append((ke, kd, kf, mu))

        (m, I) = self._compute_shape_mass(type, scale, src, density)

        self._update_body_mass(body, m, I, np.array(pos), np.array(rot))

    # particles
    def add_particle(self, pos : Vec3, vel : Vec3, mass : float) -> int:
        """Adds a single particle to the model

        Args:
            pos: The initial position of the particle
            vel: The initial velocity of the particle
            mass: The mass of the particle

        Note:
            Set the mass equal to zero to create a 'kinematic' particle that does is not subject to dynamics.

        Returns:
            The index of the particle in the system
        """
        self.particle_q.append(pos)
        self.particle_qd.append(vel)
        self.particle_mass.append(mass)

        return len(self.particle_q) - 1

    def add_spring(self, i : int, j, ke : float, kd : float, control: float):
        """Adds a spring between two particles in the system

        Args:
            i: The index of the first particle
            j: The index of the second particle
            ke: The elastic stiffness of the spring
            kd: The damping stiffness of the spring
            control: The actuation level of the spring

        Note:
            The spring is created with a rest-length based on the distance
            between the particles in their initial configuration.

        """        
        self.spring_indices.append(i)
        self.spring_indices.append(j)
        self.spring_stiffness.append(ke)
        self.spring_damping.append(kd)
        self.spring_control.append(control)

        # compute rest length
        p = self.particle_q[i]
        q = self.particle_q[j]

        delta = np.subtract(p, q)
        l = np.sqrt(np.dot(delta, delta))

        self.spring_rest_length.append(l)

    def add_triangle(self, i : int, j : int, k : int) -> float:
        """Adds a trianglular FEM element between three particles in the system. 

        Triangles are modeled as viscoelastic elements with elastic stiffness and damping
        Parameters specfied on the model. See model.tri_ke, model.tri_kd.

        Args:
            i: The index of the first particle
            j: The index of the second particle
            k: The index of the third particle

        Return:
            The area of the triangle

        Note:
            The triangle is created with a rest-length based on the distance
            between the particles in their initial configuration.

        Todo:
            * Expose elastic paramters on a per-element basis

        """      
        # compute basis for 2D rest pose
        p = np.array(self.particle_q[i])
        q = np.array(self.particle_q[j])
        r = np.array(self.particle_q[k])

        qp = q - p
        rp = r - p

        # construct basis aligned with the triangle
        n = normalize(np.cross(qp, rp))
        e1 = normalize(qp)
        e2 = normalize(np.cross(n, e1))

        R = np.matrix((e1, e2))
        M = np.matrix((qp, rp))

        D = R * M.T
        inv_D = np.linalg.inv(D)

        area = np.linalg.det(D) / 2.0

        if (area < 0.0):
            print("inverted triangle element")

        self.tri_indices.append((i, j, k))
        self.tri_poses.append(inv_D.tolist())
        self.tri_activations.append(0.0)

        return area

    def add_tetrahedron(self, i: int, j: int, k: int, l: int, k_mu: float=1.e+3, k_lambda: float=1.e+3, k_damp: float=0.0) -> float:
        """Adds a tetrahedral FEM element between four particles in the system. 

        Tetrahdera are modeled as viscoelastic elements with a NeoHookean energy
        density based on [Smith et al. 2018].

        Args:
            i: The index of the first particle
            j: The index of the second particle
            k: The index of the third particle
            l: The index of the fourth particle
            k_mu: The first elastic Lame parameter
            k_lambda: The second elastic Lame parameter
            k_damp: The element's damping stiffness

        Return:
            The volume of the tetrahedron

        Note:
            The tetrahedron is created with a rest-pose based on the particle's initial configruation

        """      
        # compute basis for 2D rest pose
        p = np.array(self.particle_q[i])
        q = np.array(self.particle_q[j])
        r = np.array(self.particle_q[k])
        s = np.array(self.particle_q[l])

        qp = q - p
        rp = r - p
        sp = s - p

        Dm = np.matrix((qp, rp, sp)).T
        volume = np.linalg.det(Dm) / 6.0

        if (volume <= 0.0):
            print("inverted tetrahedral element")
        else:

            inv_Dm = np.linalg.inv(Dm)

            self.tet_indices.append((i, j, k, l))
            self.tet_poses.append(inv_Dm.tolist())
            self.tet_activations.append(0.0)
            self.tet_mu.append(k_mu)
            self.tet_lambda.append(k_lambda)
            self.tet_damping.append(k_damp)

        return volume

    def add_edge(self, i: int, j: int, k: int, l: int, rest: float=None):
        """Adds a bending edge element between four particles in the system. 

        Bending elements are designed to be between two connected triangles. Then
        bending energy is based of [Bridson et al. 2002]. Bending stiffness is controlled
        by the `model.tri_kb` parameter.

        Args:
            i: The index of the first particle
            j: The index of the second particle
            k: The index of the third particle
            l: The index of the fourth particle
            rest: The rest angle across the edge in radians, if not specified it will be computed

        Note:
            The edge lies between the particles indexed by 'k' and 'l' parameters with the opposing
            vertices indexed by 'i' and 'j'. This defines two connected triangles with counter clockwise
            winding: (i, k, l), (j, l, k).

        """      
        # compute rest angle
        if (rest == None):

            x1 = np.array(self.particle_q[i])
            x2 = np.array(self.particle_q[j])
            x3 = np.array(self.particle_q[k])
            x4 = np.array(self.particle_q[l])

            n1 = normalize(np.cross(x3 - x1, x4 - x1))
            n2 = normalize(np.cross(x4 - x2, x3 - x2))
            e = normalize(x4 - x3)

            d = np.clip(np.dot(n2, n1), -1.0, 1.0)

            angle = math.acos(d)
            sign = np.sign(np.dot(np.cross(n2, n1), e))

            rest = angle * sign

        self.edge_indices.append((i, j, k, l))
        self.edge_rest_angle.append(rest)

    def add_cloth_grid(self,
                       pos: Vec3,
                       rot: Quat,
                       vel: Vec3,
                       dim_x: int,
                       dim_y: int,
                       cell_x: float,
                       cell_y: float,
                       mass: float,
                       reverse_winding: bool=False,
                       fix_left: bool=False,
                       fix_right: bool=False,
                       fix_top: bool=False,
                       fix_bottom: bool=False):

        """Helper to create a regular planar cloth grid

        Creates a rectangular grid of particles with FEM triangles and bending elements
        automatically.

        Args:
            pos: The position of the cloth in world space
            rot: The orientation of the cloth in world space
            vel: The velocity of the cloth in world space
            dim_x_: The number of rectangular cells along the x-axis
            dim_y: The number of rectangular cells along the y-axis
            cell_x: The width of each cell in the x-direction
            cell_y: The width of each cell in the y-direction
            mass: The mass of each particle
            reverse_winding: Flip the winding of the mesh
            fix_left: Make the left-most edge of particles kinematic (fixed in place)
            fix_right: Make the right-most edge of particles kinematic 
            fix_top: Make the top-most edge of particles kinematic
            fix_bottom: Make the bottom-most edge of particles kinematic

        """ 

        def grid_index(x, y, dim_x):
            return y * dim_x + x


        start_vertex = len(self.particle_q)
        start_tri = len(self.tri_indices)

        for y in range(0, dim_y + 1):
            for x in range(0, dim_x + 1):

                g = np.array((x * cell_x, y * cell_y, 0.0))
                p = quat_rotate(rot, g) + pos
                m = mass

                if (x == 0 and fix_left):
                    m = 0.0
                elif (x == dim_x and fix_right):
                    m = 0.0
                elif (y == 0 and fix_bottom):
                    m = 0.0
                elif (y == dim_y and fix_top):
                    m = 0.0

                self.add_particle(p, vel, m)

                if (x > 0 and y > 0):

                    if (reverse_winding):
                        tri1 = (start_vertex + grid_index(x - 1, y - 1, dim_x + 1),
                                start_vertex + grid_index(x, y - 1, dim_x + 1),
                                start_vertex + grid_index(x, y, dim_x + 1))

                        tri2 = (start_vertex + grid_index(x - 1, y - 1, dim_x + 1),
                                start_vertex + grid_index(x, y, dim_x + 1),
                                start_vertex + grid_index(x - 1, y, dim_x + 1))

                        self.add_triangle(*tri1)
                        self.add_triangle(*tri2)

                    else:

                        tri1 = (start_vertex + grid_index(x - 1, y - 1, dim_x + 1),
                                start_vertex + grid_index(x, y - 1, dim_x + 1),
                                start_vertex + grid_index(x - 1, y, dim_x + 1))

                        tri2 = (start_vertex + grid_index(x, y - 1, dim_x + 1),
                                start_vertex + grid_index(x, y, dim_x + 1),
                                start_vertex + grid_index(x - 1, y, dim_x + 1))

                        self.add_triangle(*tri1)
                        self.add_triangle(*tri2)

        end_vertex = len(self.particle_q)
        end_tri = len(self.tri_indices)

        # bending constraints, could create these explicitly for a grid but this
        # is a good test of the adjacency structure
        adj = MeshAdjacency(self.tri_indices[start_tri:end_tri], end_tri - start_tri)

        for k, e in adj.edges.items():

            # skip open edges
            if (e.f0 == -1 or e.f1 == -1):
                continue

            self.add_edge(e.o0, e.o1, e.v0, e.v1)          # opposite 0, opposite 1, vertex 0, vertex 1

    def add_cloth_mesh(self, pos: Vec3, rot: Quat, scale: float, vel: Vec3, vertices: List[Vec3], indices: List[int], density: float, edge_callback=None, face_callback=None):
        """Helper to create a cloth model from a regular triangle mesh

        Creates one FEM triangle element and one bending element for every face
        and edge in the input triangle mesh

        Args:
            pos: The position of the cloth in world space
            rot: The orientation of the cloth in world space
            vel: The velocity of the cloth in world space
            vertices: A list of vertex positions
            indices: A list of triangle indices, 3 entries per-face
            density: The density per-area of the mesh
            edge_callback: A user callback when an edge is created
            face_callback: A user callback when a face is created

        Note:

            The mesh should be two manifold.
        """

        num_tris = int(len(indices) / 3)

        start_vertex = len(self.particle_q)
        start_tri = len(self.tri_indices)

        # particles
        for i, v in enumerate(vertices):

            p = quat_rotate(rot, v * scale) + pos

            self.add_particle(p, vel, 0.0)

        # triangles
        for t in range(num_tris):

            i = start_vertex + indices[t * 3 + 0]
            j = start_vertex + indices[t * 3 + 1]
            k = start_vertex + indices[t * 3 + 2]

            if (face_callback):
                face_callback(i, j, k)

            area = self.add_triangle(i, j, k)

            # add area fraction to particles
            if (area > 0.0):

                self.particle_mass[i] += density * area / 3.0
                self.particle_mass[j] += density * area / 3.0
                self.particle_mass[k] += density * area / 3.0

        end_vertex = len(self.particle_q)
        end_tri = len(self.tri_indices)

        adj = MeshAdjacency(self.tri_indices[start_tri:end_tri], end_tri - start_tri)

        # bend constraints
        for k, e in adj.edges.items():

            # skip open edges
            if (e.f0 == -1 or e.f1 == -1):
                continue

            if (edge_callback):
                edge_callback(e.f0, e.f1)

            self.add_edge(e.o0, e.o1, e.v0, e.v1)

    def add_soft_grid(self,
                      pos: Vec3,
                      rot: Quat,
                      vel: Vec3,
                      dim_x: int,
                      dim_y: int,
                      dim_z: int,
                      cell_x: float,
                      cell_y: float,
                      cell_z: float,
                      density: float,
                      k_mu: float,
                      k_lambda: float,
                      k_damp: float,
                      fix_left: bool=False,
                      fix_right: bool=False,
                      fix_top: bool=False,
                      fix_bottom: bool=False):
        """Helper to create a rectangular tetrahedral FEM grid

        Creates a regular grid of FEM tetrhedra and surface triangles. Useful for example
        to create beams and sheets. Each hexahedral cell is decomposed into 5 
        tetrahedral elements.

        Args:
            pos: The position of the solid in world space
            rot: The orientation of the solid in world space
            vel: The velocity of the solid in world space
            dim_x_: The number of rectangular cells along the x-axis
            dim_y: The number of rectangular cells along the y-axis
            dim_z: The number of rectangular cells along the z-axis
            cell_x: The width of each cell in the x-direction
            cell_y: The width of each cell in the y-direction
            cell_z: The width of each cell in the z-direction
            density: The density of each particle
            k_mu: The first elastic Lame parameter
            k_lambda: The second elastic Lame parameter
            k_damp: The damping stiffness
            fix_left: Make the left-most edge of particles kinematic (fixed in place)
            fix_right: Make the right-most edge of particles kinematic 
            fix_top: Make the top-most edge of particles kinematic
            fix_bottom: Make the bottom-most edge of particles kinematic
        """

        start_vertex = len(self.particle_q)

        mass = cell_x * cell_y * cell_z * density

        for z in range(dim_z + 1):
            for y in range(dim_y + 1):
                for x in range(dim_x + 1):

                    v = np.array((x * cell_x, y * cell_y, z * cell_z))
                    m = mass

                    if (fix_left and x == 0):
                        m = 0.0

                    if (fix_right and x == dim_x):
                        m = 0.0

                    if (fix_top and y == dim_y):
                        m = 0.0

                    if (fix_bottom and y == 0):
                        m = 0.0

                    p = quat_rotate(rot, v) + pos

                    self.add_particle(p, vel, m)

        # dict of open faces
        faces = {}

        def add_face(i: int, j: int, k: int):
            key = tuple(sorted((i, j, k)))

            if key not in faces:
                faces[key] = (i, j, k)
            else:
                del faces[key]

        def add_tet(i: int, j: int, k: int, l: int):
            self.add_tetrahedron(i, j, k, l, k_mu, k_lambda, k_damp)

            add_face(i, k, j)
            add_face(j, k, l)
            add_face(i, j, l)
            add_face(i, l, k)

        def grid_index(x, y, z):
            return (dim_x + 1) * (dim_y + 1) * z + (dim_x + 1) * y + x

        for z in range(dim_z):
            for y in range(dim_y):
                for x in range(dim_x):

                    v0 = grid_index(x, y, z) + start_vertex
                    v1 = grid_index(x + 1, y, z) + start_vertex
                    v2 = grid_index(x + 1, y, z + 1) + start_vertex
                    v3 = grid_index(x, y, z + 1) + start_vertex
                    v4 = grid_index(x, y + 1, z) + start_vertex
                    v5 = grid_index(x + 1, y + 1, z) + start_vertex
                    v6 = grid_index(x + 1, y + 1, z + 1) + start_vertex
                    v7 = grid_index(x, y + 1, z + 1) + start_vertex

                    if (((x & 1) ^ (y & 1) ^ (z & 1))):

                        add_tet(v0, v1, v4, v3)
                        add_tet(v2, v3, v6, v1)
                        add_tet(v5, v4, v1, v6)
                        add_tet(v7, v6, v3, v4)
                        add_tet(v4, v1, v6, v3)

                    else:

                        add_tet(v1, v2, v5, v0)
                        add_tet(v3, v0, v7, v2)
                        add_tet(v4, v7, v0, v5)
                        add_tet(v6, v5, v2, v7)
                        add_tet(v5, v2, v7, v0)

        # add triangles
        for k, v in faces.items():
            self.add_triangle(v[0], v[1], v[2])

    def add_soft_mesh(self, pos: Vec3, rot: Quat, scale: float, vel: Vec3, vertices: List[Vec3], indices: List[int], density: float, k_mu: float, k_lambda: float, k_damp: float):
        """Helper to create a tetrahedral model from an input tetrahedral mesh

        Args:
            pos: The position of the solid in world space
            rot: The orientation of the solid in world space
            vel: The velocity of the solid in world space
            vertices: A list of vertex positions
            indices: A list of tetrahedron indices, 4 entries per-element
            density: The density per-area of the mesh
            k_mu: The first elastic Lame parameter
            k_lambda: The second elastic Lame parameter
            k_damp: The damping stiffness
        """
        num_tets = int(len(indices) / 4)

        start_vertex = len(self.particle_q)
        start_tri = len(self.tri_indices)

        # dict of open faces
        faces = {}

        def add_face(i, j, k):
            key = tuple(sorted((i, j, k)))

            if key not in faces:
                faces[key] = (i, j, k)
            else:
                del faces[key]

        # add particles
        for v in vertices:

            p = quat_rotate(rot, v * scale) + pos

            self.add_particle(p, vel, 0.0)

        # add tetrahedra
        for t in range(num_tets):

            v0 = start_vertex + indices[t * 4 + 0]
            v1 = start_vertex + indices[t * 4 + 1]
            v2 = start_vertex + indices[t * 4 + 2]
            v3 = start_vertex + indices[t * 4 + 3]

            volume = self.add_tetrahedron(v0, v1, v2, v3, k_mu, k_lambda, k_damp)

            # distribute volume fraction to particles
            if (volume > 0.0):

                self.particle_mass[v0] += density * volume / 4.0
                self.particle_mass[v1] += density * volume / 4.0
                self.particle_mass[v2] += density * volume / 4.0
                self.particle_mass[v3] += density * volume / 4.0

                # build open faces
                add_face(v0, v2, v1)
                add_face(v1, v2, v3)
                add_face(v0, v1, v3)
                add_face(v0, v3, v2)

        # add triangles
        for k, v in faces.items():
            try:
                self.add_triangle(v[0], v[1], v[2])
            except np.linalg.LinAlgError:
                continue

    def compute_sphere_inertia(self, density: float, r: float) -> tuple:
        """Helper to compute mass and inertia of a sphere

        Args:
            density: The sphere density
            r: The sphere radius

        Returns:

            A tuple of (mass, inertia) with inertia specified around the origin
        """

        v = 4.0 / 3.0 * math.pi * r * r * r

        m = density * v
        Ia = 2.0 / 5.0 * m * r * r

        I = np.array([[Ia, 0.0, 0.0], [0.0, Ia, 0.0], [0.0, 0.0, Ia]])

        return (m, I)

    def compute_capsule_inertia(self, density: float, r: float, l: float) -> tuple:
        """Helper to compute mass and inertia of a capsule

        Args:
            density: The capsule density
            r: The capsule radius
            l: The capsule length (full width of the interior cylinder)

        Returns:

            A tuple of (mass, inertia) with inertia specified around the origin
        """

        ms = density * (4.0 / 3.0) * math.pi * r * r * r
        mc = density * math.pi * r * r * l

        # total mass
        m = ms + mc

        # adapted from ODE
        Ia = mc * (0.25 * r * r + (1.0 / 12.0) * l * l) + ms * (0.4 * r * r + 0.375 * r * l + 0.25 * l * l)
        Ib = (mc * 0.5 + ms * 0.4) * r * r

        I = np.array([[Ib, 0.0, 0.0], [0.0, Ia, 0.0], [0.0, 0.0, Ia]])

        return (m, I)

    def compute_box_inertia(self, density: float, w: float, h: float, d: float) -> tuple:
        """Helper to compute mass and inertia of a box

        Args:
            density: The box density
            w: The box width along the x-axis
            h: The box height along the y-axis
            d: The box depth along the z-axis

        Returns:

            A tuple of (mass, inertia) with inertia specified around the origin
        """

        v = w * h * d
        m = density * v

        Ia = 1.0 / 12.0 * m * (h * h + d * d)
        Ib = 1.0 / 12.0 * m * (w * w + d * d)
        Ic = 1.0 / 12.0 * m * (w * w + h * h)

        I = np.array([[Ia, 0.0, 0.0], [0.0, Ib, 0.0], [0.0, 0.0, Ic]])

        return (m, I)

    def _compute_shape_mass(self, type, scale, src, density):
      
        if density == 0:     # zero density means fixed
            return 0, np.zeros((3, 3))

        if (type == GEO_SPHERE):
            return self.compute_sphere_inertia(density, scale[0])
        elif (type == GEO_BOX):
            return self.compute_box_inertia(density, scale[0] * 2.0, scale[1] * 2.0, scale[2] * 2.0)
        elif (type == GEO_CAPSULE):
            return self.compute_capsule_inertia(density, scale[0], scale[1] * 2.0)
        elif (type == GEO_MESH):
            #todo: non-uniform scale of inertia tensor
            s = scale[0]     # eventually want to compute moment of inertia for mesh.
            return (density * src.mass * s * s * s, density * src.I * s * s * s * s * s)

    
    # incrementally updates rigid body mass with additional mass and inertia expressed at a local to the body
    def _update_body_mass(self, i, m, I, p, q):
        
        if (i == -1):
            return
            
        # find new COM
        new_mass = self.body_mass[i] + m

        if new_mass == 0.0:    # no mass
            return

        new_com = (self.body_com[i] * self.body_mass[i] + p * m) / new_mass

        # shift inertia to new COM
        com_offset = new_com - self.body_com[i]
        shape_offset = new_com - p

        new_inertia = transform_inertia(self.body_mass[i], self.body_inertia[i], com_offset, quat_identity()) + transform_inertia(
            m, I, shape_offset, q)

        self.body_mass[i] = new_mass
        self.body_inertia[i] = new_inertia
        self.body_com[i] = new_com

    def prepare_cut_python(self,
                           tet_indices,
                           surface_triangles,
                           cut_spring_ke=100.,
                           cut_spring_kd=10.,
                           cut_spring_rest_length=0.0,
                           surface_cut_spring_ke=100.,
                           surface_cut_spring_kd=10.,
                           surface_cut_spring_rest_length=0.0,
                           contact_ke=1e3,
                           contact_kd=10.,
                           contact_kf=0.1,
                           contact_mu=0.5,
                           surface_contact_ke=1e3,
                           surface_contact_kd=10.,
                           surface_contact_kf=0.1,
                           surface_contact_mu=0.5,
                           cut_spring_softness=0.1,
                           surface_cut_spring_softness=0.1,
                           verbose=False):
        # for now we assume the mesh has not been cut before
        assert (len(self.cut_edge_coords) == 0)

        import tqdm
        # top = MeshTopology(tet_indices)
        # self.tet_edge_indices = list(eid for eid in top.unique_edges)
        # return

        X = self.particle_q
        print("particles before cut:", len(X))
        # maps intersected edge to index in self.cut_edge_coords
        edge_intersections = dict()
        tet_indices_here = copy(tet_indices)

        surface = surface_triangles
        surface_min = surface[0][0]
        surface_max = surface[0][0]
        # compute bounding box of cutting surface
        for tri in surface_triangles:
            for p in tri:
                surface_min = np.min([surface_min, p], axis=0)
                surface_max = np.max([surface_max, p], axis=0)

        def edge_intersects_bounds(edge):
            return max(edge[0][0], edge[1][0]) >= surface_min[0] \
                and surface_max[0] >= min(edge[0][0], edge[1][0]) \
                and max(edge[0][1], edge[1][1]) >= surface_min[1] \
                and surface_max[1] >= min(edge[0][1], edge[1][1]) \
                and max(edge[0][2], edge[1][2]) >= surface_min[2] \
                and surface_max[2] >= min(edge[0][2], edge[1][2])

        def is_above_triangle(point, tri):
            shape = np.matrix((tri[1] - tri[0], tri[2] - tri[0], point - tri[0]))
            return np.linalg.det(shape) > 0

        def edge_intersects_tri(edge, tri, tol=1e-8):
            # Möller–Trumbore algorithm
            edge1 = tri[1] - tri[0]
            edge2 = tri[2] - tri[0]
            direction = edge[1] - edge[0]
            h = np.cross(direction, edge2)
            a = np.dot(edge1, h)
            if -tol < a < tol:
                # ray is parallel to tri
                return None
            f = 1.0 / a
            s = edge[0] - tri[0]
            u = f * np.dot(s, h)
            if u < -tol or u > 1.0 + tol:
                return None
            q = np.cross(s, edge1)
            v = f * np.dot(direction, q)
            if v < -tol or u + v > 1.0 + tol:
                return None
            # compute t to find intersection point
            t = f * np.dot(edge2, q)
            if t < -tol or t > 1.0 + tol:
                return None
            return t

        def canonical(indices):
            return tuple(sorted(indices))

        def copy_vertex(id):
            new_id = len(self.particle_q)
            self.particle_q.append(copy(self.particle_q[id]))
            self.particle_qd.append(copy(self.particle_qd[id]))
            self.particle_mass.append(copy(self.particle_mass[id]))
            return new_id

        def copy_tet(id, new_indices):
            new_id = len(tet_indices)
            tet_indices.append(copy(new_indices))
            tet_indices_here.append(copy(new_indices))
            self.tet_poses.append(copy(self.tet_poses[id]))
            self.tet_activations.append(copy(self.tet_activations[id]))
            self.tet_mu.append(copy(self.tet_mu[id]))
            self.tet_lambda.append(copy(self.tet_lambda[id]))
            self.tet_damping.append(copy(self.tet_damping[id]))
            return new_id

        def add_edge_intersection(i, j, t, p):
            eis = canonical((i, j))
            vid = len(self.cut_edge_coords)
            # store i, j in original order to ensure the barycentric coordinate t matches
            self.cut_edge_indices.append((i, j))
            self.cut_edge_coords.append(t)
            edge_intersections[eis] = (p, vid)
            return vid

        def compute_normal(tri):
            return np.cross(tri[1] - tri[0], tri[2] - tri[0])

        # counts of intersecting edges per tet
        intersections_per_tet = defaultdict(int)
        # IDs vertices that are above the surface
        above_surface = set()
        # mapping of vertices to duplicated vertices below the cut
        new_vs = dict()
        # intersecting edges with indices as they were before the topological cut
        affected_edges = set()
        # normals of cutting surface per edge
        cut_normals = dict()
        # normals of triangles on the boundary of the mesh
        boundary_normals = dict()

        top = MeshTopology(tet_indices)
        surface_edges = top.surface_edges()
        for eis in top.unique_edges.keys():
            edge = (X[eis[0]], X[eis[1]])
            if not edge_intersects_bounds(edge):
                continue
            for tri in surface:
                t = edge_intersects_tri(edge, tri)
                if t is None:
                    continue
                if verbose:
                    print("Edge", eis, "intersects at t =", t)
                affected_edges.add(eis)

                if eis[0] not in new_vs:
                    new_vs[eis[0]] = copy_vertex(eis[0])
                    self.contactless_particles.add(new_vs[eis[0]])
                if eis[1] not in new_vs:
                    new_vs[eis[1]] = copy_vertex(eis[1])
                    self.contactless_particles.add(new_vs[eis[1]])

                for tet_id in top.elements_per_edge[eis]:
                    intersections_per_tet[tet_id] += 1

                # li = add_edge_intersection(eis[0], new_vs[eis[1]], t, p)
                # ri = add_edge_intersection(new_vs[eis[0]], eis[1], t, p)
                # self.cut_spring_indices.append((li, ri))

                tri_normal = compute_normal(tri)
                cut_normals[canonical((eis[0], new_vs[eis[1]]))] = tri_normal
                cut_normals[canonical((new_vs[eis[0]], eis[1]))] = tri_normal
                self.cut_spring_normal.append(tri_normal / np.linalg.norm(tri_normal))

                p = (1.0 - t) * edge[0] + t * edge[1]
                if is_above_triangle(edge[0], tri):
                    above_surface.add(eis[0])
                    above_surface.add(new_vs[eis[0]])
                    # edge[0] is always the side connected to the mesh, i.e. opposite to cutting surface
                    li = add_edge_intersection(eis[0], new_vs[eis[1]], t, p)
                    ri = add_edge_intersection(new_vs[eis[0]], eis[1], t, p)
                else:
                    above_surface.add(eis[1])
                    above_surface.add(new_vs[eis[1]])
                    # edge[0] is always the side connected to the mesh, i.e. opposite to cutting surface
                    li = add_edge_intersection(eis[1], new_vs[eis[0]], 1. - t, p)
                    ri = add_edge_intersection(new_vs[eis[1]], eis[0], 1. - t, p)
                self.cut_spring_indices.append((li, ri))
                # XXX now each spring has contact parameters
                if eis in surface_edges:
                    self.sdf_ke.append(surface_contact_ke)
                    self.sdf_kd.append(surface_contact_kd)
                    self.sdf_kf.append(surface_contact_kf)
                    self.sdf_mu.append(surface_contact_mu)
                    self.cut_spring_rest_length.append(surface_cut_spring_rest_length)
                    self.cut_spring_stiffness.append(surface_cut_spring_ke)
                    self.cut_spring_damping.append(surface_cut_spring_kd)
                    self.cut_spring_softness.append(surface_cut_spring_softness)
                else:
                    self.sdf_ke.append(contact_ke)
                    self.sdf_kd.append(contact_kd)
                    self.sdf_kf.append(contact_kf)
                    self.sdf_mu.append(contact_mu)
                    self.cut_spring_rest_length.append(cut_spring_rest_length)
                    self.cut_spring_stiffness.append(cut_spring_ke)
                    self.cut_spring_damping.append(cut_spring_kd)
                    self.cut_spring_softness.append(cut_spring_softness)

                # we have found an intersection for this edge
                break

        print("particles after cut (cut_vertex_offset):", len(X))
        # index after which the added intersection vertices are added in the visualization nodes
        cut_vertex_offset = len(X)
        cut_tets = set(self.cut_tets)
        intersected_tris = set()
        original_tri_indices = {canonical(tri): i for i, tri in enumerate(self.tri_indices)}

        if verbose:
            progress = affected_edges
        else:
            progress = tqdm.tqdm(affected_edges)
        for eis in progress:
            for tet_id in top.elements_per_edge[eis]:
                if tet_id in cut_tets:
                    continue
                cut_tets.add(tet_id)
                tet = tet_indices[tet_id]
                if 1 <= intersections_per_tet[tet_id] < 3:
                    # this tet is partially cut
                    # TODO maintain separate list for these?
                    continue

                # tet where vertices above cut remain fixed
                tet_above = (
                    tet[0] if tet[0] in above_surface else new_vs[tet[0]],
                    tet[1] if tet[1] in above_surface else new_vs[tet[1]],
                    tet[2] if tet[2] in above_surface else new_vs[tet[2]],
                    tet[3] if tet[3] in above_surface else new_vs[tet[3]],
                )
                # newly added tet where vertices below cut remain fixed
                tet_below = (
                    new_vs[tet[0]] if tet[0] in above_surface else tet[0],
                    new_vs[tet[1]] if tet[1] in above_surface else tet[1],
                    new_vs[tet[2]] if tet[2] in above_surface else tet[2],
                    new_vs[tet[3]] if tet[3] in above_surface else tet[3],
                )

                # store previous boundary triangle normals
                tet_tri_indices = MeshTopology.face_indices(tet)
                above_tris = MeshTopology.face_indices(tet_above)
                below_tris = MeshTopology.face_indices(tet_below)
                for orig_tri, above_tri, below_tri in zip(tet_tri_indices, above_tris, below_tris):
                    if orig_tri not in original_tri_indices:
                        continue
                    # we have a boundary triangle
                    intersected_tris.add(orig_tri)
                    tri_idx = self.tri_indices[original_tri_indices[orig_tri]]
                    tri = (X[tri_idx[0]], X[tri_idx[1]], X[tri_idx[2]])
                    normal = compute_normal(tri)
                    boundary_normals[above_tri] = normal
                    boundary_normals[below_tri] = normal

                if verbose:
                    print("tet before:", tet_indices[tet_id])
                tet_indices[tet_id] = tet_above
                if verbose:
                    print("tet after: ", tet_above)
                tet_below_id = copy_tet(tet_id, tet_below)
                cut_tets.add(tet_below_id)

                def insert_tri(tri, tri_indices, normal, above):
                    # whether the vertices are only represented by hard particles
                    nx = len(self.particle_q)
                    is_only_virtual = all(np.array(tri_indices) >= nx)
                    if verbose:
                        print("Inserting triangle", tri_indices, " - cutoff:", nx)
                    # insert cutting triangle with correct winding number so the triangle normal
                    # points in direction of the provided normal
                    if np.dot(compute_normal(tri), normal) > 0:
                        self.cut_tri_indices.append(tri_indices)
                        if is_only_virtual:
                            if verbose:
                                print("Add virtual-only triangle", tri_indices, "with normal", normal)
                            self.cut_virtual_tri_indices.append(np.array(tri_indices) - nx)
                            if above:
                                self.cut_virtual_tri_indices_above_cut.append(np.array(tri_indices) - nx)
                            else:
                                self.cut_virtual_tri_indices_below_cut.append(np.array(tri_indices) - nx)
                    else:
                        self.cut_tri_indices.append(tri_indices[::-1])
                        if is_only_virtual:
                            if verbose:
                                print("Add FLIPPED virtual-only triangle", tri_indices[::-1], "with normal", normal)
                            self.cut_virtual_tri_indices.append(np.array(tri_indices[::-1]) - nx)
                            if above:
                                self.cut_virtual_tri_indices_above_cut.append(np.array(tri_indices[::-1]) - nx)
                            else:
                                self.cut_virtual_tri_indices_below_cut.append(np.array(tri_indices[::-1]) - nx)

                def triangulate_poly(polygon, normal, above):
                    # triangulate polygons with 3 or 4 vertices
                    if len(polygon) == 0:
                        return
                    if not (3 <= len(polygon) <= 4):
                        return
                    assert (3 <= len(polygon) <= 4)
                    if verbose:
                        print("Triangulate with normal =", normal)
                    idxs = list(polygon.keys())
                    vecs = list(polygon.values())
                    if len(polygon) == 3:
                        insert_tri(vecs, idxs, normal, above)
                        return
                    assert (len(polygon) == 4)
                    # consider 3 cases of triangulation:
                    # 1. (0, 1, 2) and (0, 2, 3)
                    # 2. (0, 1, 2) and (0, 1, 3)
                    # 3. (0, 1, 3) and (0, 2, 3)
                    v1 = vecs[1] - vecs[0]
                    v2 = vecs[2] - vecs[0]
                    v3 = vecs[3] - vecs[0]
                    if np.dot(np.cross(v2, v3), np.cross(v1, v2)) > 0.0:
                        # case 1
                        insert_tri(vecs[:3], idxs[:3], normal, above)
                        insert_tri((vecs[0], vecs[2], vecs[3]), (idxs[0], idxs[2], idxs[3]), normal, above)
                    elif np.dot(np.cross(v1, v2), np.cross(v3, v1)) > 0.0:
                        # case 2
                        insert_tri(vecs[:3], idxs[:3], normal, above)
                        insert_tri((vecs[0], vecs[1], vecs[3]), (idxs[0], idxs[1], idxs[3]), normal, above)
                    else:
                        # case 3
                        insert_tri((vecs[0], vecs[1], vecs[3]), (idxs[0], idxs[1], idxs[3]), normal, above)
                        insert_tri((vecs[0], vecs[2], vecs[3]), (idxs[0], idxs[2], idxs[3]), normal, above)

                def add_polygons(tet, above):
                    cut_polygon = OrderedDict()  # polygon at cutting interface
                    avg_normal = np.zeros(3)
                    for face in MeshTopology.face_indices(tet):
                        polygon = OrderedDict()  # cut face polygon of tet
                        for a, b in ((face[0], face[1]), (face[1], face[2]), (face[2], face[0])):
                            eid = canonical((a, b))
                            if eid in cut_normals:
                                avg_normal = cut_normals[eid]
                            edge_is_cut = eid in edge_intersections
                            above_a = a in above_surface
                            above_b = b in above_surface
                            if not above:
                                above_a = not above_a
                                above_b = not above_b
                            if not above_a and not above_b:
                                continue
                            if above_a and above_b:
                                polygon[a] = X[a]
                                polygon[b] = X[b]
                            else:
                                if not edge_is_cut:
                                    if verbose:
                                        print("Warning: no intersection information for edge (%i,%i)" % (a, b), file=sys.stderr)
                                    polygon[a] = X[a]
                                    polygon[b] = X[b]
                                    continue
                                p, ab = edge_intersections[eid]
                                if above_a:
                                    polygon[a] = X[a]
                                    polygon[cut_vertex_offset + ab] = p
                                else:
                                    polygon[cut_vertex_offset + ab] = p
                                    polygon[b] = X[b]
                                cut_polygon[cut_vertex_offset + ab] = p
                        if face in boundary_normals:
                            triangulate_poly(polygon, boundary_normals[face], above)
                    triangulate_poly(cut_polygon, avg_normal if above else -avg_normal, above)

                # triangulate intersecting tets (faces, cutting interface)
                add_polygons(tet_above, True)
                add_polygons(tet_below, False)

        self.cut_tets = list(cut_tets)
        # remove previous intersecting faces for this tet
        self.tri_indices = [tri for tri in self.tri_indices if canonical(tri) not in intersected_tris]
        top = MeshTopology(tet_indices_here)
        self.tet_edge_indices = list(eid for eid in top.unique_edges if eid not in edge_intersections)

        self.cut_duplicated_x = new_vs

    # cuts the tets by the triangular surface, duplicating intersecting tets, inserting vertices on intersecting edges,
    # and adding springs between former and duplicated vertices
    def prepare_cut(self,
                    tet_indices,
                    surface_triangles,
                    cut_spring_ke=100.,
                    cut_spring_kd=10.,
                    cut_spring_rest_length=0.0,
                    surface_cut_spring_ke=100.,
                    surface_cut_spring_kd=10.,
                    surface_cut_spring_rest_length=0.0,
                    contact_ke=1e3,
                    contact_kd=10.,
                    contact_kf=0.1,
                    contact_mu=0.5,
                    surface_contact_ke=1e3,
                    surface_contact_kd=10.,
                    surface_contact_kf=0.1,
                    surface_contact_mu=0.5,
                    cut_spring_softness=0.1,
                    surface_cut_spring_softness=0.1,
                    verbose=False,
                    use_cpp=True):

        self.original_tri_indices = self.tri_indices
        import dflex as df

        if use_cpp:
            from meshcutter import MeshCutter
            mc = MeshCutter(self.tri_indices, tet_indices, self.particle_q)
            mc.verbose = verbose
            mc.triangle_test_tolerance = 0.0
            try:
                with df.ScopedTimer("cut_meshing_cpp", True):
                    success = mc.cut(surface_triangles)
                if not success:
                    print("Error(s) occurred during meshing.")
                if mc.verbose:
                    with open("meshing_log.txt", "w") as f:
                        f.write(mc.log())
                    print("Saved log file to meshing_log.txt")
            except Exception as e:
                print(mc.log())
                print("Error:", e, file=sys.stderr)
                with open("meshing_log.txt", "w") as f:
                    f.write(mc.log())
                print("Saved log file to meshing_log.txt")

            if len(mc.cut_tri_indices) > 0:
                self.cut_tri_indices = np.array(mc.cut_tri_indices)
                self.cut_virtual_tri_indices_above_cut = np.array(mc.cut_virtual_tri_indices_above_cut)
                self.cut_virtual_tri_indices_below_cut = np.array(mc.cut_virtual_tri_indices_below_cut)
                self.particle_q = np.array(mc.particle_x)
                self.cut_edge_coords = np.array(mc.cut_edge_coords)
                self.cut_edge_indices = np.array(mc.cut_edge_indices)
                self.cut_virtual_tri_indices = np.array(mc.cut_virtual_tri_indices)
                self.cut_spring_indices = np.array(mc.cut_spring_indices)
                self.cut_spring_normal = np.array(mc.cut_spring_normal)

                self.contactless_particles = set(mc.contactless_particles)

                # new_id starts from the previous number of particle_q
                for new_id, old_id in mc.vertex_copy_from.items():
                    self.particle_qd.append(copy(self.particle_qd[old_id]))
                    self.particle_mass.append(copy(self.particle_mass[old_id]))
                # same for tet IDs
                for new_id, old_id in mc.tet_copy_from.items():
                    self.tet_poses.append(copy(self.tet_poses[old_id]))
                    self.tet_activations.append(copy(self.tet_activations[old_id]))
                    self.tet_mu.append(copy(self.tet_mu[old_id]))
                    self.tet_lambda.append(copy(self.tet_lambda[old_id]))
                    self.tet_damping.append(copy(self.tet_damping[old_id]))

                # remove previous intersecting faces for this tet
                self.tri_indices = np.array(mc.tri_indices)
                self.tet_indices = np.array(mc.tet_indices)

                assert (len(self.particle_q) == len(self.particle_qd))
                assert (len(self.tet_indices) == len(self.tet_mu))

                num_springs = len(self.cut_spring_indices)
                surface_springs = np.array(mc.cut_spring_indices_surface)
                self.sdf_ke = np.repeat(contact_ke, num_springs)
                self.sdf_ke[surface_springs] = surface_contact_ke
                self.sdf_kd = np.repeat(contact_kd, num_springs)
                self.sdf_kd[surface_springs] = surface_contact_kd
                self.sdf_kf = np.repeat(contact_kf, num_springs)
                self.sdf_kf[surface_springs] = surface_contact_kf
                self.sdf_mu = np.repeat(contact_mu, num_springs)
                self.sdf_mu[surface_springs] = surface_contact_mu

                self.cut_spring_rest_length = np.repeat(cut_spring_rest_length, num_springs)
                self.cut_spring_rest_length[surface_springs] = surface_cut_spring_rest_length
                self.cut_spring_stiffness = np.repeat(cut_spring_ke, num_springs)
                self.cut_spring_stiffness[surface_springs] = surface_cut_spring_ke
                self.cut_spring_damping = np.repeat(cut_spring_kd, num_springs)
                self.cut_spring_damping[surface_springs] = surface_cut_spring_kd
                self.cut_spring_softness = np.repeat(cut_spring_softness, num_springs)
                self.cut_spring_softness[surface_springs] = surface_cut_spring_softness
                self.cut_tets = np.array(mc.intersected_tets())

                self.cut_duplicated_x = mc.duplicated_x
            else:
                raise Exception("No cut was made.")
        else:
            with df.ScopedTimer("cut_meshing_python", True):
                self.prepare_cut_python(tet_indices,
                                        surface_triangles,
                                        cut_spring_ke=cut_spring_ke,
                                        cut_spring_kd=cut_spring_kd,
                                        cut_spring_rest_length=cut_spring_rest_length,
                                        surface_cut_spring_ke=surface_cut_spring_ke,
                                        surface_cut_spring_kd=surface_cut_spring_kd,
                                        surface_cut_spring_rest_length=surface_cut_spring_rest_length,
                                        contact_ke=contact_ke,
                                        contact_kd=contact_kd,
                                        contact_kf=contact_kf,
                                        contact_mu=contact_mu,
                                        surface_contact_ke=surface_contact_ke,
                                        surface_contact_kd=surface_contact_kd,
                                        surface_contact_kf=surface_contact_kf,
                                        surface_contact_mu=surface_contact_mu,
                                        cut_spring_softness=cut_spring_softness,
                                        surface_cut_spring_softness=surface_cut_spring_softness,
                                        verbose=verbose)

        # self.tet_edge_indices = list(eid for eid in top.surface_edges() if eid not in edge_intersections)
        assert len(self.cut_virtual_tri_indices_above_cut) == len(self.cut_virtual_tri_indices_below_cut), "Number of virtual triangles above and below the cut should match."

        print(f'{len(self.cut_spring_indices)} cut springs have been inserted.')

    # returns a (model, state) pair given the description
    def finalize(self, adapter: str, knife = None, minimum_mass=0.0, requires_grad=True) -> Model:
        """Convert this builder object to a concrete model for simulation.

        After building simulation elements this method should be called to transfer
        all data to PyTorch tensors ready for simulation.

        Args:
            adapter: The simulation adapter to use, e.g.: 'cpu', 'cuda'

        Returns:

            A model object.
        """

        if minimum_mass > 0.0:
            for i in range(len(self.particle_mass)):
                if (self.particle_mass[i] > 0.0):
                    self.particle_mass[i] = max(minimum_mass, self.particle_mass[i])
        # construct particle inv masses
        particle_inv_mass = []
        for m in self.particle_mass:
            if (m > 0.0):
                particle_inv_mass.append(1.0 / m)
            else:
                particle_inv_mass.append(0.0)

        #-------------------------------------
        # construct Model (non-time varying) data

        m = Model(adapter)

        #---------------------        
        # particles

        # state (initial)
        m.particle_q = torch.tensor(self.particle_q, dtype=torch.float32, device=adapter)
        m.particle_qd = torch.tensor(self.particle_qd, dtype=torch.float32, device=adapter)

        # model 
        m.particle_mass = torch.tensor(self.particle_mass, dtype=torch.float32, device=adapter)
        m.particle_inv_mass = torch.tensor(particle_inv_mass, dtype=torch.float32, device=adapter)

        #---------------------
        # collision geometry

        m.shape_transform = torch.tensor(transform_flatten_list(self.shape_transform), dtype=torch.float32, device=adapter)
        m.shape_body = torch.tensor(self.shape_body, dtype=torch.int32, device=adapter)
        m.shape_geo_type = torch.tensor(self.shape_geo_type, dtype=torch.int32, device=adapter)
        m.shape_geo_src = self.shape_geo_src
        m.shape_geo_scale = torch.tensor(self.shape_geo_scale, dtype=torch.float32, device=adapter)
        m.shape_materials = torch.tensor(self.shape_materials, dtype=torch.float32, device=adapter)

        #---------------------
        # springs

        m.spring_indices = torch.tensor(self.spring_indices, dtype=torch.int32, device=adapter)
        m.spring_rest_length = torch.tensor(self.spring_rest_length, dtype=torch.float32, device=adapter)
        m.spring_stiffness = torch.tensor(self.spring_stiffness, dtype=torch.float32, device=adapter)
        m.spring_damping = torch.tensor(self.spring_damping, dtype=torch.float32, device=adapter)
        m.spring_control = torch.tensor(self.spring_control, dtype=torch.float32, device=adapter)

        #---------------------
        # triangles

        m.tri_indices = torch.tensor(self.tri_indices, dtype=torch.int32, device=adapter)
        m.tri_poses = torch.tensor(self.tri_poses, dtype=torch.float32, device=adapter)
        m.tri_activations = torch.tensor(self.tri_activations, dtype=torch.float32, device=adapter)

        #---------------------
        # edges

        m.edge_indices = torch.tensor(self.edge_indices, dtype=torch.int32, device=adapter)
        m.edge_rest_angle = torch.tensor(self.edge_rest_angle, dtype=torch.float32, device=adapter)

        #---------------------
        # tetrahedra

        m.tet_indices = torch.tensor(self.tet_indices, dtype=torch.int32, device=adapter)
        m.tet_poses = torch.tensor(self.tet_poses, dtype=torch.float32, device=adapter)
        m.tet_activations = torch.tensor(self.tet_activations, dtype=torch.float32, device=adapter)
        m.tet_mu = torch.tensor(self.tet_mu, dtype=torch.float32, device=adapter)
        m.tet_lambda = torch.tensor(self.tet_lambda, dtype=torch.float32, device=adapter)
        m.tet_damping = torch.tensor(self.tet_damping, dtype=torch.float32, device=adapter)

        #-----------------------
        # muscles

        muscle_count = len(self.muscle_start)

        # close the muscle waypoint indices
        self.muscle_start.append(len(self.muscle_links))

        m.muscle_start = torch.tensor(self.muscle_start, dtype=torch.int32, device=adapter)
        m.muscle_params = torch.tensor(self.muscle_params, dtype=torch.float32, device=adapter)
        m.muscle_links = torch.tensor(self.muscle_links, dtype=torch.int32, device=adapter)
        m.muscle_points = torch.tensor(self.muscle_points, dtype=torch.float32, device=adapter)
        m.muscle_activation = torch.tensor(self.muscle_activation, dtype=torch.float32, device=adapter)

        #--------------------------------------
        # articulations

        # build 6x6 spatial inertia and COM transform
        body_X_cm = []
        body_I_m = [] 

        for i in range(len(self.body_inertia)):
            body_I_m.append(spatial_matrix_from_inertia(self.body_inertia[i], self.body_mass[i]))
            body_X_cm.append(transform(self.body_com[i], quat_identity()))
        
        m.body_I_m = torch.tensor(body_I_m, dtype=torch.float32, device=adapter)


        articulation_count = len(self.articulation_start)
        joint_coord_count = len(self.joint_q)
        joint_dof_count = len(self.joint_qd)

        # 'close' the start index arrays with a sentinel value
        self.joint_q_start.append(len(self.joint_q))
        self.joint_qd_start.append(len(self.joint_qd))
        self.articulation_start.append(len(self.joint_type))        

        # calculate total size and offsets of Jacobian and mass matrices for entire system
        m.J_size = 0
        m.M_size = 0
        m.H_size = 0

        articulation_J_start = []
        articulation_M_start = []
        articulation_H_start = []

        articulation_M_rows = []
        articulation_H_rows = []
        articulation_J_rows = []
        articulation_J_cols = []

        articulation_dof_start = []
        articulation_coord_start = []

        for i in range(articulation_count):

            first_joint = self.articulation_start[i]
            last_joint = self.articulation_start[i+1]

            first_coord = self.joint_q_start[first_joint]
            last_coord = self.joint_q_start[last_joint]

            first_dof = self.joint_qd_start[first_joint]
            last_dof = self.joint_qd_start[last_joint]

            joint_count = last_joint-first_joint
            dof_count = last_dof-first_dof
            coord_count = last_coord-first_coord

            articulation_J_start.append(m.J_size)
            articulation_M_start.append(m.M_size)
            articulation_H_start.append(m.H_size)
            articulation_dof_start.append(first_dof)
            articulation_coord_start.append(first_coord)

            # bit of data duplication here, but will leave it as such for clarity
            articulation_M_rows.append(joint_count*6)
            articulation_H_rows.append(dof_count)
            articulation_J_rows.append(joint_count*6)
            articulation_J_cols.append(dof_count)

            m.J_size += 6*joint_count*dof_count
            m.M_size += 6*joint_count*6*joint_count
            m.H_size += dof_count*dof_count
            

        m.articulation_joint_start = torch.tensor(self.articulation_start, dtype=torch.int32, device=adapter)

        # matrix offsets for batched gemm
        m.articulation_J_start = torch.tensor(articulation_J_start, dtype=torch.int32, device=adapter)
        m.articulation_M_start = torch.tensor(articulation_M_start, dtype=torch.int32, device=adapter)
        m.articulation_H_start = torch.tensor(articulation_H_start, dtype=torch.int32, device=adapter)
        
        m.articulation_M_rows = torch.tensor(articulation_M_rows, dtype=torch.int32, device=adapter)
        m.articulation_H_rows = torch.tensor(articulation_H_rows, dtype=torch.int32, device=adapter)
        m.articulation_J_rows = torch.tensor(articulation_J_rows, dtype=torch.int32, device=adapter)
        m.articulation_J_cols = torch.tensor(articulation_J_cols, dtype=torch.int32, device=adapter)

        m.articulation_dof_start = torch.tensor(articulation_dof_start, dtype=torch.int32, device=adapter)
        m.articulation_coord_start = torch.tensor(articulation_coord_start, dtype=torch.int32, device=adapter)

        # state (initial)
        m.joint_q = torch.tensor(self.joint_q, dtype=torch.float32, device=adapter)
        m.joint_qd = torch.tensor(self.joint_qd, dtype=torch.float32, device=adapter)

        # model
        m.joint_type = torch.tensor(self.joint_type, dtype=torch.int32, device=adapter)
        m.joint_parent = torch.tensor(self.joint_parent, dtype=torch.int32, device=adapter)
        m.joint_X_pj = torch.tensor(transform_flatten_list(self.joint_X_pj), dtype=torch.float32, device=adapter)
        m.joint_X_cm = torch.tensor(transform_flatten_list(body_X_cm), dtype=torch.float32, device=adapter)
        m.joint_axis = torch.tensor(self.joint_axis, dtype=torch.float32, device=adapter)
        m.joint_q_start = torch.tensor(self.joint_q_start, dtype=torch.int32, device=adapter) 
        m.joint_qd_start = torch.tensor(self.joint_qd_start, dtype=torch.int32, device=adapter)

        # dynamics properties
        m.joint_armature = torch.tensor(self.joint_armature, dtype=torch.float32, device=adapter)
        
        m.joint_target = torch.tensor(self.joint_target, dtype=torch.float32, device=adapter)
        m.joint_target_ke = torch.tensor(self.joint_target_ke, dtype=torch.float32, device=adapter)
        m.joint_target_kd = torch.tensor(self.joint_target_kd, dtype=torch.float32, device=adapter)

        m.joint_limit_lower = torch.tensor(self.joint_limit_lower, dtype=torch.float32, device=adapter)
        m.joint_limit_upper = torch.tensor(self.joint_limit_upper, dtype=torch.float32, device=adapter)
        m.joint_limit_ke = torch.tensor(self.joint_limit_ke, dtype=torch.float32, device=adapter)
        m.joint_limit_kd = torch.tensor(self.joint_limit_kd, dtype=torch.float32, device=adapter)

        # counts
        m.particle_count = len(self.particle_q)

        m.articulation_count = articulation_count
        m.joint_coord_count = joint_coord_count
        m.joint_dof_count = joint_dof_count
        m.muscle_count = muscle_count

        m.link_count = len(self.joint_type)        
        m.shape_count = len(self.shape_geo_type)
        m.tri_count = len(self.tri_poses)
        m.tet_count = len(self.tet_poses)
        m.edge_count = len(self.edge_rest_angle)
        m.spring_count = len(self.spring_rest_length)
        m.contact_count = 0
        
        # store refs to geometry
        m.geo_meshes = self.geo_meshes
        m.geo_sdfs = self.geo_sdfs

        # enable ground plane
        m.ground = True
        m.enable_tri_collisions = False

        m.gravity = torch.tensor((0.0, -9.80665, 0.0), dtype=torch.float32, device=adapter)

        # allocate space for mass / jacobian matrices
        m.alloc_mass_matrix()

        # cutting data
        m.cut_edge_indices = torch.tensor(self.cut_edge_indices, dtype=torch.int32, device=adapter)
        m.cut_edge_count = len(self.cut_edge_indices)
        m.cut_edge_coords = torch.tensor(self.cut_edge_coords, dtype=torch.float32, device=adapter, requires_grad=requires_grad)
        m.cut_tri_indices = torch.tensor(self.cut_tri_indices, dtype=torch.int32, device=adapter)
        m.sdf_ke = torch.tensor(self.sdf_ke, dtype=torch.float32, device=adapter, requires_grad=requires_grad)
        m.sdf_kd = torch.tensor(self.sdf_kd, dtype=torch.float32, device=adapter, requires_grad=requires_grad)
        m.sdf_kf = torch.tensor(self.sdf_kf, dtype=torch.float32, device=adapter, requires_grad=requires_grad)
        m.sdf_mu = torch.tensor(self.sdf_mu, dtype=torch.float32, device=adapter, requires_grad=requires_grad)
        m.sdf_radius = torch.tensor(self.sdf_radius, dtype=torch.float32, device=adapter, requires_grad=requires_grad)
        m.cut_tri_count = len(self.cut_tri_indices)
        m.cut_virtual_tri_indices = torch.tensor(self.cut_virtual_tri_indices, dtype=torch.int32, device=adapter)
        m.cut_virtual_tri_count = len(self.cut_virtual_tri_indices)
        m.cut_virtual_tri_indices_above_cut = torch.tensor(self.cut_virtual_tri_indices_above_cut, dtype=torch.int32, device=adapter)
        m.cut_virtual_tri_indices_below_cut = torch.tensor(self.cut_virtual_tri_indices_below_cut, dtype=torch.int32, device=adapter)
        m.cut_spring_indices = torch.tensor(self.cut_spring_indices, dtype=torch.int32, device=adapter)
        m.cut_spring_normal = torch.tensor(self.cut_spring_normal, dtype=torch.float32, device=adapter, requires_grad=requires_grad)
        m.cut_spring_rest_length = torch.tensor(self.cut_spring_rest_length, dtype=torch.float32, device=adapter, requires_grad=requires_grad)
        m.cut_spring_stiffness = torch.tensor(self.cut_spring_stiffness, dtype=torch.float32, device=adapter, requires_grad=requires_grad)
        m.cut_spring_damping = torch.tensor(self.cut_spring_damping, dtype=torch.float32, device=adapter, requires_grad=requires_grad)
        m.cut_spring_softness = torch.tensor(self.cut_spring_softness, dtype=torch.float32, device=adapter, requires_grad=requires_grad)
        m.cut_spring_count = len(self.cut_spring_indices)

        m.knife_link_index = self.knife_link_index

        m.knife_tri_indices = torch.tensor(self.knife_tri_indices, dtype=torch.int32, device=adapter)
        m.knife_tri_count = len(self.knife_tri_indices) // 3
        m.knife_tri_vertices = torch.tensor(self.knife_tri_vertices, dtype=torch.float32, device=adapter)

        # coupling springs
        m.coupling_spring_count = len(self.coupling_spring_indices)
        m.coupling_spring_indices = torch.tensor(self.coupling_spring_indices, dtype=torch.int32, device=adapter)  # [rigid_body_index, particle_index]
        m.coupling_spring_moment_arm = torch.tensor(self.coupling_spring_moment_arm, dtype=torch.float32, device=adapter, requires_grad=requires_grad)
        m.coupling_spring_stiffness = torch.tensor(self.coupling_spring_stiffness, dtype=torch.float32, device=adapter, requires_grad=requires_grad)
        m.coupling_spring_damping = torch.tensor(self.coupling_spring_damping, dtype=torch.float32, device=adapter, requires_grad=requires_grad)

        # dependent particles
        m.dependent_particle_count = len(self.dependent_particle_indices)
        m.dependent_particle_indices = torch.tensor(self.dependent_particle_indices, dtype=torch.int32, device=adapter)  # [rigid_body_index, particle_index]
        m.dependent_particle_moment_arm = torch.tensor(self.dependent_particle_moment_arm, dtype=torch.float32, device=adapter, requires_grad=requires_grad)

        if knife is not None:
            m.knife_params = torch.tensor([knife.spine_dim, knife.spine_height, knife.edge_dim, knife.tip_height, knife.depth],
                                        dtype=torch.float32,
                                        device=adapter,
                                        requires_grad=requires_grad)

        # contact coords store barycentric edge coordinate of contact between edge and knife
        m.cut_edge_contact_coord = torch.tensor(np.zeros(len(self.cut_edge_indices)), dtype=torch.float32, device=adapter, requires_grad=requires_grad)
        m.cut_edge_contact_dist = torch.tensor(np.ones(len(self.cut_edge_indices)), dtype=torch.float32, device=adapter, requires_grad=requires_grad)
        m.cut_edge_contact_normal = torch.tensor(np.zeros((len(self.cut_edge_indices), 3)), dtype=torch.float32, device=adapter, requires_grad=requires_grad)

        print("self.cut_edge_indices:", np.shape(self.cut_edge_indices))
        print("self.cut_spring_indices:", np.shape(self.cut_spring_indices))
        print("self.cut_virtual_tri_indices:", np.shape(self.cut_virtual_tri_indices))

        m.contact_mask = torch.tensor([(0.0 if i in self.contactless_particles else 1.0) for i in range(len(self.particle_q))],
                                      dtype=torch.float32,
                                      device=adapter)

        return m
