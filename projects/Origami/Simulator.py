from reader import *
from physics.fixed_corotated import *
from common.math.math_tools import *
from common.utils.timer import *
from common.utils.logger import *

import sys, os, time, math
import taichi as ti
import taichi_three as t3
import numpy as np
import meshio
import pickle
import scipy.sparse
import scipy.sparse.linalg
from sksparse.cholmod import *
from dihedral_angle import *

##############################################################################
testcase = int(sys.argv[1])
settings = read()
mesh_particles = settings['mesh_particles']
mesh_elements = settings['mesh_elements']
mesh_edges = settings['mesh_edges']
dim = 3
codim = 2
gravity = settings['gravity']
bending_weight = 1.0
thickness = 0.0003

##############################################################################
directory = 'output/' + '_'.join(sys.argv[:2]) + '/'
os.makedirs(directory + 'images/', exist_ok=True)
os.makedirs(directory + 'caches/', exist_ok=True)
os.makedirs(directory + 'objs/', exist_ok=True)
print('output directory:', directory)
# sys.stdout = open(directory + 'log.txt', 'w')
# sys.stderr = open(directory + 'err.txt', 'w')

##############################################################################
real = ti.f64
ti.init(arch=ti.cpu, default_fp=real) #, cpu_max_num_threads=1)

scalar = lambda: ti.field(real)
vec = lambda: ti.Vector.field(dim, real)
mat = lambda: ti.Matrix.field(dim, dim, real)
mat2 = lambda: ti.Matrix.field(codim, codim, real)

dt = 0.04
sub_steps = 1
n_particles = len(mesh_particles)
n_elements = len(mesh_elements)
n_edges = len(mesh_edges)

x, x0, xPrev, xTilde, xn, v, m = vec(), vec(), vec(), vec(), vec(), vec(), scalar()
zero = vec()
la, mu = scalar(), scalar()
restT = mat()
B = mat2()
vertices = ti.field(ti.i32)
edges = ti.field(ti.i32)
rest_angle = ti.field(real)
rest_e = ti.field(real)
rest_h = ti.field(real)
ti.root.dense(ti.i, n_particles).place(x, x0, xPrev, xTilde, xn, v, m)
ti.root.dense(ti.i, n_particles).place(zero)
ti.root.dense(ti.i, n_elements).place(la, mu)
ti.root.dense(ti.i, n_elements).place(restT)
ti.root.dense(ti.i, n_elements).place(B)
ti.root.dense(ti.ij, (n_elements, dim)).place(vertices)
ti.root.dense(ti.ij, (n_edges, dim + 2)).place(edges)
ti.root.dense(ti.i, n_edges).place(rest_angle, rest_e, rest_h)

MAX_LINEAR = 50000000 if dim == 3 else 5000000
data_rhs = ti.field(real, shape=n_particles * dim)
data_row = ti.field(ti.i32, shape=MAX_LINEAR)
data_col = ti.field(ti.i32, shape=MAX_LINEAR)
data_val = ti.field(real, shape=MAX_LINEAR)
data_sol = ti.field(real, shape=n_particles * dim)
cnt = ti.field(ti.i32, shape=())
dfx = ti.field(ti.i32, shape=n_particles * dim)

@ti.func
def compute_density(i):
    return 800.


@ti.func
def compute_lame_parameters(i):
    E = 3e9
    nu = 0.3
    return E * nu / ((1 + nu) * (1 - 2 * nu)), E / (2 * (1 + nu))


@ti.kernel
def compute_filter_3D_inversion_free() -> real:
    alpha = 1.0
    for i in range(n_elements):
        a, b, c, d = vertices[i, 0], vertices[i, 1], vertices[i, 2], vertices[i, 3]
        da = ti.Vector([data_sol[a * dim + 0], data_sol[a * dim + 1], data_sol[a * dim + 2]])
        db = ti.Vector([data_sol[b * dim + 0], data_sol[b * dim + 1], data_sol[b * dim + 2]])
        dc = ti.Vector([data_sol[c * dim + 0], data_sol[c * dim + 1], data_sol[c * dim + 2]])
        dd = ti.Vector([data_sol[d * dim + 0], data_sol[d * dim + 1], data_sol[d * dim + 2]])
        alpha = min(alpha, get_smallest_positive_real_cubic_root(x[a], x[b], x[c], x[d], da, db, dc, dd, 0.2))
    return alpha


@ti.func
def compute_T(i):
    if ti.static(codim == 2):
        ab = x[vertices[i, 1]] - x[vertices[i, 0]]
        ac = x[vertices[i, 2]] - x[vertices[i, 0]]
        T = ti.Matrix([[ab[0], ac[0]], [ab[1], ac[1]]])
        return T.transpose() @ T


@ti.kernel
def compute_restT_and_m():
    for i in range(n_elements):
        restT[i] = compute_T(i)
        B[i] = restT[i].inverse()
        mass = thickness * restT[i].determinant() / codim / (codim - 1) * compute_density(i) / (codim + 1)
        if mass < 0.0:
            print("FATAL ERROR : mesh inverted")
        for d in ti.static(range(dim + 1)):
            m[vertices[i, d]] += mass
        la[i], mu[i] = compute_lame_parameters(i)
        
    for i in range(n_edges):
        if edges[i, 4] < 0: continue
        rest_angle[i] = 0.0
        rest_e[i] = (x[edges[i, 0]] - x[edges[i, 1]]).norm()
        x0 = x[edges[i, 2]]
        x1 = x[edges[i, 0]]
        x2 = x[edges[i, 1]]
        x3 = x[edges[i, 3]]
        n1 = ti.cross(x1 - x0, x2 - x0)
        n2 = ti.cross(x2 - x3, x1 - x3)
        rest_h[i] = (n1.norm() + n2.norm()) / (rest_e[i] * 6)


@ti.kernel
def compute_xn_and_xTilde():
    for i in range(n_particles):
        xn[i] = x[i]
        xTilde[i] = x[i] + dt * v[i]
        xTilde(1)[i] += dt * dt * gravity


def move_nodes(current_time):
    dirichlet_fixed, dirichlet_value = settings['dirichlet'](current_time)
    for i in range(n_particles):
        if dirichlet_fixed[i]:
            for d in range(dim):
                x(d)[i] = dirichlet_value[i, d]
                xTilde(d)[i] = dirichlet_value[i, d]


@ti.kernel
def compute_energy() -> real:
    total_energy = 0.0
    # inertia
    for i in range(n_particles):
        total_energy += 0.5 * m[i] * (x[i] - xTilde[i]).norm_sqr()
    
    # membrane
    for e in range(n_elements):
        F = compute_T(e) @ B[e]
        lnJ = 0.5 * ti.log(F.determinant())
        mem = 0.5 * mu[e] * (F.trace() - 3 - 2 * lnJ) + 0.5 * la[e] * lnJ * lnJ
        vol0 = thickness * restT[e].determinant() / codim / (codim - 1)
        total_energy += mem * dt * dt * vol0
    
    # bending
    for e in range(n_edges):
        if edges[i, 4] < 0: continue
        x0 = x[edges[i, 2]]
        x1 = x[edges[i, 0]]
        x2 = x[edges[i, 1]]
        x3 = x[edges[i, 3]]
        theta = dihedral_angle(x0, x1, x2, x3)
        ben = (theta - rest_angle[e]) * (theta - rest_angle[e]) * rest_e[e] / rest_h[e]
        total_energy += bending_weight * dt * dt * ben
    
    return total_energy

@ti.kernel
def compute_gradient():
    # inertia
    for i in range(n_particles):
        for d in ti.static(range(dim)):
            data_rhs[i * dim + d] -= m[i] * (x(d)[i] - xTilde(d)[i])

    # membrane
    for e in range(n_elements):
        vol0 = thickness * restT[e].determinant() / codim / (codim - 1)
        x1, x2, x3 = x[vertices[e, 0]], x[vertices[e, 1]], x[vertices[e, 2]]
        A = compute_T(e)
        IA = A.inverse()
        lnJ = 0.5 * ti.log(A.determinant() * B[e].determinant())
        de_div_dA = dt * dt * vol0 * ((0.5 * mu[e] * IB + 0.5 * (-mu[e] + la[e] * lnJ) * IA))
        Z = ti.Vector([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        dA_div_dx = ti.Matrix.rows([Z, Z, Z, Z])
        for d in ti.static(range(3)):
            dA_div_dx[0, 3 + i] += 2.0 * (x2[i] - x1[i])
            dA_div_dx[0, 0 + i] -= 2.0 * (x2[i] - x1[i])
            dA_div_dx[1, 6 + i] += (x2[i] - x1[i])
            dA_div_dx[1, 3 + i] += (x3[i] - x1[i])
            dA_div_dx[1, 0 + i] += - (x2[i] - x1[i]) - (x3[i] - x1[i])
            dA_div_dx[2, 6 + i] += (x2[i] - x1[i])
            dA_div_dx[2, 3 + i] += (x3[i] - x1[i])
            dA_div_dx[2, 0 + i] += - (x2[i] - x1[i]) - (x3[i] - x1[i])
            dA_div_dx[3, 6 + i] += 2.0 * (x3[i] - x1[i])
            dA_div_dx[3, 0 + i] -= 2.0 * (x3[i] - x1[i])
        for endI in ti.static(range(3)):
            for dimI in ti.static(range(3)):
                i = endI * dim + dimI
                global_i = 3 * vertices[e, endI] + dimI
                data_rhs[global_i] -= dA_div_dx[0, i] * de_div_dA[0, 0]
                                    + dA_div_dx[1, i] * de_div_dA[1, 0]
                                    + dA_div_dx[2, i] * de_div_dA[0, 1]
                                    + dA_div_dx[3, i] * de_div_dA[1, 1]

    # bending
    for e in range(n_edges):
        if edges[e, 4] < 0: continue
        x0 = x[edges[e, 2]]
        x1 = x[edges[e, 0]]
        x2 = x[edges[e, 1]]
        x3 = x[edges[e, 3]]
        theta = dihedral_angle(x0, x1, x2, x3)
        grad = dihedral_angle_gradient(x0, x1, x2, x3)
        grad *= bending_weight * dt * dt * 2 * (theta - rest_angle[e]) * rest_e[e] / rest_h[e]
        for d in ti.static(range(3)):
            data_rhs[3 * edges[i, 2] + d] -= grad[0 * 3 + d]
            data_rhs[3 * edges[i, 0] + d] -= grad[1 * 3 + d]
            data_rhs[3 * edges[i, 1] + d] -= grad[2 * 3 + d]
            data_rhs[3 * edges[i, 3] + d] -= grad[3 * 3 + d]

@ti.kernel
def compute_hessian(pd: bool):
    cnt[None] = 0
    #inertia
    for i in range(n_particles):
        for d in ti.static(range(dim)):
            c = cnt[None] + i * dim + d
            data_row[c] = i * dim + d
            data_col[c] = i * dim + d
            data_val[c] = m[i]
    cnt[None] += n_particles * dim

    Z = ti.Vector([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    ahess = [ti.Matrix.rows([Z, Z, Z, Z, Z, Z, Z, Z, Z]), 
             ti.Matrix.rows([Z, Z, Z, Z, Z, Z, Z, Z, Z]), 
             ti.Matrix.rows([Z, Z, Z, Z, Z, Z, Z, Z, Z]), 
             ti.Matrix.rows([Z, Z, Z, Z, Z, Z, Z, Z, Z])]
    for d in ti.static(range(dim)):
        ahess[0][0 + d, 0 + d] += 2.0
        ahess[0][3 + d, 3 + d] += 2.0
        ahess[0][0 + d, 3 + d] -= 2.0
        ahess[0][3 + d, 0 + d] -= 2.0

        ahess[1][3 + d, 6 + d] += 1.0
        ahess[1][6 + d, 3 + d] += 1.0
        ahess[1][0 + d, 3 + d] -= 1.0
        ahess[1][0 + d, 6 + d] -= 1.0
        ahess[1][3 + d, 0 + d] -= 1.0
        ahess[1][6 + d, 0 + d] -= 1.0
        ahess[1][0 + d, 0] + d += 2.0

        ahess[2][3 + d, 6 + d] += 1.0
        ahess[2][6 + d, 3 + d] += 1.0
        ahess[2][0 + d, 3 + d] -= 1.0
        ahess[2][0 + d, 6 + d] -= 1.0
        ahess[2][3 + d, 0 + d] -= 1.0
        ahess[2][6 + d, 0 + d] -= 1.0
        ahess[2][0 + d, 0 + d] += 2.0

        ahess[3][0 + d, 0 + d] += 2.0
        ahess[3][6 + d, 6 + d] += 2.0
        ahess[3][0 + d, 6 + d] -= 2.0
        ahess[3][6 + d, 0 + d] -= 2.0

    #membrane
    for e in range(n_elements):
        vol0 = thickness * restT[e].determinant() / codim / (codim - 1)
        x1, x2, x3 = x[vertices[e, 0]], x[vertices[e, 1]], x[vertices[e, 2]]
        Z = ti.Vector([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        dA_div_dx = ti.Matrix.rows([Z, Z, Z, Z])
        for i in ti.static(range(3)):
            dA_div_dx[0, 3 + i] += 2.0 * (x2[i] - x1[i])
            dA_div_dx[0, 0 + i] -= 2.0 * (x2[i] - x1[i])
            dA_div_dx[1, 6 + i] += (x2[i] - x1[i])
            dA_div_dx[1, 3 + i] += (x3[i] - x1[i])
            dA_div_dx[1, 0 + i] += - (x2[i] - x1[i]) - (x3[i] - x1[i])
            dA_div_dx[2, 6 + i] += (x2[i] - x1[i])
            dA_div_dx[2, 3 + i] += (x3[i] - x1[i])
            dA_div_dx[2, 0 + i] += - (x2[i] - x1[i]) - (x3[i] - x1[i])
            dA_div_dx[3, 6 + i] += 2.0 * (x3[i] - x1[i])
            dA_div_dx[3, 0 + i] -= 2.0 * (x3[i] - x1[i])
        
        hessian = ti.Matrix.rows([Z, Z, Z, Z, Z, Z, Z, Z, Z])
        A = compute_T(e)
        IA = A.inverse()
        de_div_dA = dt * dt * vol0 * ((0.5 * mu[e] * B[e] + 0.5 * (-mu[e] + la[e] * lnJ) * IA))
        ainvda = ti.Vector([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        for endI in ti.static(range(3)):
            for dimI in ti.static(range(3)):
                i = endI * dim + dimI
                ainvda[i] = dA_div_dx[0, i] * IA[0, 0] + dA_div_dx[1, i] * IA[1, 0] + dA_div_dx[2, i] * IA[0, 1] + dA_div_dx[3, i] * IA[1, 1]
            }
        }
        deta = A.determinant()
        lnJ = 0.5 * ti.log(deta * B[e].determinant())
        term1 = (-mu[e] + la[e] * lnJ) * 0.5
        hessian = (-term1 + la[e] / 4) * ti.outer_product(ainvda, ainvda)

        aderivadj = ti.Matrix.rows([Z, Z, Z, Z])
        for d in ti.static(range(9)):
            aderivadj[0, d] = dA_div_dx[3, d]
            aderivadj[1, d] = - dA_div_dx[1, d]
            aderivadj[2, d] = - dA_div_dx[2, d]
            aderivadj[3, d] = dA_div_dx[0, d]

        hessian += term1 / deta * aderivadj.transpose() @ dA_div_dx

        for i in ti.static(range(2)):
            for j in ti.static(range(2)):
                hessian += (term1 * IA[i, j] + mu[e] / 2 * IB[i, j]) * ahess[i + j * 2]
        
        inner00 = ti.Vector([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        inner01 = ti.Vector([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        inner10 = ti.Vector([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        inner11 = ti.Vector([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        for d in ti.static(range(9)):
            inner00[d] = IB[0, 0] * dA_div_dx[0, d] + IB[0, 1] * dA_div_dx[2, d]
            inner01[d] = IB[0, 0] * dA_div_dx[1, d] + IB[0, 1] * dA_div_dx[3, d]
            inner10[d] = IB[1, 0] * dA_div_dx[0, d] + IB[1, 1] * dA_div_dx[2, d]
            inner11[d] = IB[1, 0] * dA_div_dx[1, d] + IB[1, 1] * dA_div_dx[3, d]
        hessian += 2 * mu[e] * ti.outer_product(inner00, inner00)
        hessian += 2 * mu[e] * (ti.outer_product(inner01, inner10) + ti.outer_product(inner10, inner01))
        hessian += 2 * mu[e] * ti.outer_product(inner11, inner11)
        
        hessian *= dt * dt * vol0 / 4
    
        if pd:
            project_pd(hessian)
        
        indMap = ti.Vector([vertices[e, 0] * 3, vertices[e, 0] * 3 + 1, vertices[e, 0] * 3 + 2,
                            vertices[e, 1] * 3, vertices[e, 1] * 3 + 1, vertices[e, 1] * 3 + 2,
                            vertices[e, 2] * 3, vertices[e, 2] * 3 + 1, vertices[e, 2] * 3 + 2])
        for i in ti.static(range(9)):
            for j in ti.static(range(9)):
                c = c[None] + e * 81 + i * 9 + j
                data_row[c], data_col[c], data_val[c] = indMap[i], indMap[j], hessian[i, j]
    c[None] += 81 * n_elements

    # bending
    for e in range(n_edges):
        if edges[e, 4] < 0: continue
        x0 = x[edges[e, 2]]
        x1 = x[edges[e, 0]]
        x2 = x[edges[e, 1]]
        x3 = x[edges[e, 3]]
        theta = dihedral_angle(x0, x1, x2, x3)
        grad = dihedral_angle_gradient(x0, x1, x2, x3)
        H = dihedral_angle_hessian(x0, x1, x2, x3)
        H *= dt * dt * bending_weight * 2.0 * (theta - rest_angle[e]) * rest_e[e] / rest_h[e]
        H += (dt * dt * bending_weight * 2.0 * rest_e[e] / rest_h[e]) * ti.outer_product(grad, grad)
        if pd: 
            project_pd(H)
        indMap = ti.Vector([edges[e, 2] * 3, edges[e, 2] * 3 + 1, edges[e, 2] * 3 + 2,
                            edges[e, 0] * 3, edges[e, 0] * 3 + 1, edges[e, 0] * 3 + 2,
                            edges[e, 1] * 3, edges[e, 1] * 3 + 1, edges[e, 1] * 3 + 2,
                            edges[e, 3] * 3, edges[e, 3] * 3 + 1, edges[e, 3] * 3 + 2])
        for i in ti.static(range(12)):
            for j in ti.static(range(12)):
                c = c[None] + e * 144 + i * 12 + j
                data_row[c], data_col[c], data_val[c] = indMap[i], indMap[j], H[i, j]
    c[None] += 144 * n_elements
    

def compute_hessian_and_gradient(pd: bool):
    compute_gradient()
    compute_hessian(pd)

def solve_system(current_time):
    dirichlet_fixed, dirichlet_value = settings['dirichlet'](current_time)
    D, V = np.stack((dirichlet_fixed,) * dim, axis=-1).reshape((n_particles * dim)), np.zeros((n_particles * dim))
    if cnt[None] >= MAX_LINEAR:
        print("FATAL ERROR: Array Too Small!")
    print("Total entries: ", cnt[None])
    with Timer("DBC 0"):
        dfx.from_numpy(D.astype(np.int32))
        @ti.kernel
        def DBC_set_zeros():
            for i in range(cnt[None]):
                if dfx[data_row[i]] or dfx[data_col[i]]:
                    data_val[i] = 0
        DBC_set_zeros()
    with Timer("Taichi to numpy"):
        row, col, val = data_row.to_numpy()[:cnt[None]], data_col.to_numpy()[:cnt[None]], data_val.to_numpy()[:cnt[None]]
        rhs = data_rhs.to_numpy()
    with Timer("DBC 1"):
        n = n_particles * dim
        A = scipy.sparse.csr_matrix((val, (row, col)), shape=(n, n))
        D = np.where(D)[0]
        A += scipy.sparse.csr_matrix((np.ones(len(D)), (D, D)), shape=(n, n))
        rhs[D] = 0
    with Timer("System Solve"):
        factor = cholesky(A)
        sol = factor(rhs)
    with Timer("Numpy to taichi"):
        data_sol.from_numpy(sol)


@ti.kernel
def save_x0():
    for i in range(n_particles):
        x0[i] = x[i]


@ti.kernel
def save_xPrev():
    for i in range(n_particles):
        xPrev[i] = x[i]


@ti.kernel
def apply_sol(alpha : real):
    for i in range(n_particles):
        for d in ti.static(range(dim)):
            x(d)[i] = xPrev(d)[i] + data_sol[i * dim + d] * alpha


@ti.kernel
def compute_v():
    for i in range(n_particles):
        v[i] = (x[i] - xn[i]) / dt


@ti.kernel
def output_residual() -> real:
    residual = 0.0
    for i in range(n_particles):
        for d in ti.static(range(dim)):
            residual = max(residual, ti.abs(data_sol[i * dim + d]))
    print("Search Direction Residual : ", residual / dt)
    return residual


if dim == 2:
    gui = ti.GUI("FEM", (768, 768), background_color=0x112F41)
else:
    scene = t3.Scene()
    model = t3.Model(f_n=n_boundary_triangles, vi_n=n_particles)
    scene.add_model(model)
    camera = t3.Camera((768, 768))
    scene.add_camera(camera)
    light = t3.Light([0.4, -1.5, 1.8])
    scene.add_light(light)
    gui = ti.GUI('FEM', camera.res)

def write_image(f):
    particle_pos = x.to_numpy() * settings['mesh_scale'] + settings['mesh_offset']
    x_ = x.to_numpy()
    vertices_ = vertices.to_numpy()
    if dim == 2:
        for i in range(n_elements):
            for j in range(3):
                a, b = vertices_[i, j], vertices_[i, (j + 1) % 3]
                gui.line((particle_pos[a][0], particle_pos[a][1]),
                         (particle_pos[b][0], particle_pos[b][1]),
                         radius=1,
                         color=0x4FB99F)
        if 'visualize_segments' in settings:
            for a, b in settings['visualize_segments']:
                gui.line((particle_pos[a][0], particle_pos[a][1]),
                         (particle_pos[b][0], particle_pos[b][1]),
                         radius=1,
                         color=0xFFB99F)
        gui.show(directory + f'images/{f:06d}.png')
    else:
        model.vi.from_numpy(particle_pos.astype(np.float32))
        model.faces.from_numpy(boundary_triangles_.astype(np.int32))
        camera.from_mouse(gui)
        scene.render()
        gui.set_image(camera.img)
        gui.show(directory + f'images/{f:06d}.png')
        f = open(directory + f'objs/{f:06d}.obj', 'w')
        for i in range(n_particles):
            f.write('v %.6f %.6f %.6f\n' % (x_[i, 0], x_[i, 1], x_[i, 2]))
        for [p0, p1, p2] in boundary_triangles_:
            f.write('f %d %d %d\n' % (p0 + 1, p1 + 1, p2 + 1))
        f.close()


if __name__ == "__main__":
    with Logger(directory + f'log.txt'):
        x.from_numpy(mesh_particles.astype(np.float64))
        v.fill(0)
        vertices.from_numpy(mesh_elements.astype(np.int32))
        edges.from_numpy(mesh_edges.astype(np.int32))
        compute_restT_and_m()
        save_x0()
        zero.fill(0)
        write_image(0)
        f_start = 0
        if len(sys.argv) == 3:
            f_start = int(sys.argv[2])
            [x_, v_] = pickle.load(open(directory + f'caches/{f_start:06d}.p', 'rb'))
            x.from_numpy(x_)
            v.from_numpy(v_)
        newton_iter_total = 0
        current_time = 0
        for f in range(f_start, 10000):
            with Timer("Time Step"):
                print("==================== Frame: ", f, " ====================")
                for step in range(sub_steps):
                    print("=============== Step: ", step, " =================")
                    compute_xn_and_xTilde()
                    move_nodes(current_time)
                    newton_iter = 0
                    while True:
                        newton_iter += 1
                        print("-------------------- Newton Iteration: ", newton_iter, " --------------------")
                        with Timer("Build System"):
                            data_row.fill(0)
                            data_col.fill(0)
                            data_val.fill(0)
                            data_rhs.fill(0)
                            data_sol.fill(0)
                            compute_hessian_and_gradient()
                        with Timer("Solve System"):
                            solve_system(current_time)
                        if output_residual() < 1e-3 * dt:
                            break
                        with Timer("Line Search"):
                            E0 = compute_energy()
                            save_xPrev()
                            alpha = 1
                            apply_sol(alpha)
                            E = compute_energy()
                            while E > E0:
                                alpha *= 0.5
                                apply_sol(alpha)
                                E = compute_energy()
                                print(E, E0)
                            print("[Step size after line search: ", alpha, "]")
                    compute_v()
                    current_time += dt
                    newton_iter_total += newton_iter
                print("Avg Newton iter: ", newton_iter_total / (f + 1))
            with Timer("Visualization"):
                write_image(f + 1)
            pickle.dump([x.to_numpy(), v.to_numpy()], open(directory + f'caches/{f + 1:06d}.p', 'wb'))
            Timer_Print()