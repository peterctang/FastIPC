import taichi as ti
import math
import time

real = ti.f32

@ti.data_oriented
class MPMSolver:
    def __init__(self):
        self.n_particles = ti.field(ti.i32, shape=())
        self.dim = 3
        self.dt = 5e-6
        self.dx = 0.00001
        self.inv_dx = 1. / self.dx
        self.p_vol = (self.dx * 0.5) ** self.dim
        self.p_rho = 8909.
        self.p_mass = self.p_vol * self.p_rho
        self.res = 512

        #################### particles data ####################
        # position
        self.x = ti.Vector.field(self.dim, dtype=real)
        # temperature
        self.T = ti.field(real)
        # temperature with last PIC transfer
        self.T_ = ti.field(real)
        # liquid/solid status: 0--solid 1--liquid
        self.S = ti.field(ti.i32)
        # latent heat buffer
        self.LH = ti.field(real)
        self.particle = ti.root.dynamic(ti.i, 2**27, 2**20)
        self.particle.place(self.x, self.T, self.T_, self.S, self.LH)

        #################### grid data ####################
        indices = ti.ij if self.dim == 2 else ti.ijk
        self.grid_size = 4096
        self.offset = tuple(-self.grid_size // 2 for _ in range(self.dim))
        self.pid = ti.field(ti.i32)
        # color function for existing particles
        self.grid_color = ti.field(ti.i32)
        self.grid_H = ti.field(real)
        self.grid_theta = ti.field(real)
        self.grid_delta = ti.field(real)
        grid_block_size = 128
        self.grid = ti.root.pointer(indices, self.grid_size // grid_block_size)
        self.leaf_block_size = 16 if self.dim == 2 else 8
        block = self.grid.pointer(indices, grid_block_size // self.leaf_block_size)
        def block_component(c):
            block.dense(indices, self.leaf_block_size).place(c, offset=self.offset)
        block_component(self.grid_color)
        block_component(self.grid_H)
        block_component(self.grid_theta)
        block_component(self.grid_delta)
        block.dynamic(ti.indices(self.dim),
                      1024 * 1024,
                      chunk_size=self.leaf_block_size**self.dim * 8).place(
            self.pid, offset=self.offset + (0, ))

        #################### visualization data ####################
        self.img = ti.field(dtype=real, shape=(self.res, self.res))

    def stencil_range(self):
        return ti.ndrange(*((3, ) * self.dim))

    @ti.func
    def get_heat_capacity(self, T):
        result = 0.
        if T < 400.:
            result = 0.7 * 1e3 + (0.54 - 0.7) * (T - 298) / (400 - 298) * 1e3
        elif T < 1728.:
            result = 0.54 * 1e3 + (0.61 - 0.54) * (T - 400) / (1728 - 400) * 1e3
        else:
            result = 0.75 * 1e3
        return result

    @ti.func
    def get_thermal_conductivity(self, T):
        result = 0.
        if T < 1728.:
            result = 56. + (85. - 64.) * (T - 298.) / (1728. - 298.)
        else:
            result = 70.
        return result

    @ti.func
    def get_electrical_resistivity(self, T):
        result = 0.
        if T < 1728.:
            result = (0.23 + (0.54 - 0.23) * (T - 298) / (1728 - 298)) * 1e-6
        elif T < 3100:
            result = (0.82767 + (0.92349 - 0.82767) * (T - 1728) / (3100 - 1728)) * 1e-6
        else:
            result = 0.92349 * 1e-6
        return result

    @ti.kernel
    def sample_particles(self):
        lower = ti.Vector([-0.00075, -0.0012, -0.00015])
        upper = ti.Vector([0.00075, 0.0003, 0.00015])
        cell_size = self.dx * 0.5
        print("?>>> ", *(ti.cast((upper - lower) / cell_size, ti.i32)))
        for I in ti.grouped(ti.ndrange(*(ti.cast((upper - lower) / cell_size, ti.i32)))):
            p = ti.atomic_add(self.n_particles[None], 1)
            d = ti.Vector([ti.random(real) for i in range(self.dim)])
            self.x[p] = lower + (ti.cast(I, real) + d) * cell_size
            self.T[p] = 297.
            self.T_[p] = 297.
            self.S[p] = 0
            self.LH[p] = 0.
        print("Total particle#", self.n_particles[None])

    @ti.kernel
    def build_pid(self):
        ti.block_dim(64)
        for p in self.x:
            base = int(ti.floor(self.x[p] * self.inv_dx - 0.5))
            ti.append(self.pid.parent(), base - ti.Vector(list(self.offset)), p)


    @ti.kernel
    def step0(self):
        # prepare
        ti.no_activate(self.particle)
        ti.block_dim(256)
        for I in ti.grouped(self.pid):
            p = self.pid[I]
            T = self.T[p]
            c = self.get_heat_capacity(T)
            base_2 = ti.floor(self.x[p] * self.inv_dx - 0.5).cast(int)
            fx = self.x[p] * self.inv_dx - base_2.cast(real)
            w = [0.5 * (1.5 - fx)**2, 0.75 - (fx - 1)**2, 0.5 * (fx - 0.5)**2]
            for offset in ti.static(ti.grouped(self.stencil_range())):
                weight = w[offset[0]][0] * w[offset[1]][1] * w[offset[2]][2]
                self.grid_H[base_2 + offset] += self.p_mass * c * weight
                self.grid_theta[base_2 + offset] += self.p_mass * c * T * weight
            base_0 = ti.floor(self.x[p] * self.inv_dx).cast(int)
            self.grid_color[base_0] = 1

    @ti.kernel
    def step1(self):
        for I in ti.grouped(self.grid_H):
            if self.grid_H[I] > 0:
                self.grid_theta[I] = (1 / self.grid_H[I]) * self.grid_theta[I]

    @ti.kernel
    def step2(self, laser_on: real):
        ti.no_activate(self.particle)
        ti.block_dim(256)
        for I in ti.grouped(self.pid):
            p = self.pid[I]
            T = self.T[p]
            kappa = self.get_thermal_conductivity(T)
            base_2 = ti.floor(self.x[p] * self.inv_dx - 0.5).cast(int)
            fx = self.x[p] * self.inv_dx - base_2.cast(real)
            w = [0.5 * (1.5 - fx)**2, 0.75 - (fx - 1)**2, 0.5 * (fx - 0.5)**2]
            dw = [fx - 1.5, -2.0 * (fx - 1), fx - 0.5]

            middle = ti.Matrix.zero(real, self.dim, 1)
            for offset in ti.static(ti.grouped(self.stencil_range())):
                dweight = ti.Vector([
                    self.inv_dx * dw[offset[0]][0] * w[offset[1]][1] * w[offset[2]][2],
                    self.inv_dx * w[offset[0]][0] * dw[offset[1]][1] * w[offset[2]][2],
                    self.inv_dx * w[offset[0]][0] * w[offset[1]][1] * dw[offset[2]][2]])
                middle += dweight * self.grid_theta[base_2 + offset]

            for offset in ti.static(ti.grouped(self.stencil_range())):
                dweight = ti.Vector([
                    self.inv_dx * dw[offset[0]][0] * w[offset[1]][1] * w[offset[2]][2],
                    self.inv_dx * w[offset[0]][0] * dw[offset[1]][1] * w[offset[2]][2],
                    self.inv_dx * w[offset[0]][0] * w[offset[1]][1] * dw[offset[2]][2]])
                self.grid_delta[base_2 + offset] -= kappa * self.p_vol * dweight.dot(middle)

            base_0 = ti.floor(self.x[p] * self.inv_dx).cast(int)
            base_0[1] += 1
            beta = 0.01
            convection = -80.0 * (T - 298.)
            radiation = -0.3 * 5.670367 * 1e-8 * (T**4 - 298.**4)
            evaporation = -beta * 6100000. * 101325. * ti.exp(-(1/T - 1/3003) * 6100000. * 0.0586934 / 8.3145) * ti.sqrt(0.0586934 / 2 / math.pi / 8.3145 / T)
            r = ti.sqrt(self.x[p](0)**2 + self.x[p](2)**2)
            r0 = 0.000064
            t = self.get_electrical_resistivity(T) / (1070.0 * 1e-9)
            alpha = 0.365 * (t**0.5) - 0.0667 * t + 0.006 * (t**1.5)
            laser = (2. * alpha * 197) / (math.pi * (r0**2)) * ti.exp((-2 * (r**2)) / (r0**2)) * laser_on
            for offset in ti.static(ti.grouped(self.stencil_range())):
                weight = w[offset[0]][0] * w[offset[1]][1] * w[offset[2]][2]
                self.grid_delta[base_2 + offset] += (self.dx ** 2) / 4 * weight * (convection + radiation + evaporation + laser) * (1 - self.grid_color[base_0])

        m1, m2, m3 = 0., 0., 0.
        for I in ti.grouped(self.grid_H):
            ti.atomic_max(m1, self.grid_delta[I])
            ti.atomic_max(m2, self.grid_theta[I])
            ti.atomic_max(m3, self.grid_H[I])

    @ti.kernel
    def step3(self):
        ti.no_activate(self.particle)
        ti.block_dim(256)
        for I in ti.grouped(self.pid):
            p = self.pid[I]
            base = ti.floor(self.x[p] * self.inv_dx - 0.5).cast(int)
            fx = self.x[p] * self.inv_dx - base.cast(float)
            # Quadratic kernels  [http://mpm.graphics   Eqn. 123, with x=fx, fx-1,fx-2]
            w = [0.5 * (1.5 - fx)**2, 0.75 - (fx - 1)**2, 0.5 * (fx - 0.5)**2]
            T, T_ = 0., 0.
            for offset in ti.static(ti.grouped(self.stencil_range())):
                weight = w[offset[0]][0] * w[offset[1]][1] * w[offset[2]][2]
                delta = self.grid_delta[base + offset]
                H = self.grid_H[base + offset]
                theta = self.grid_theta[base + offset]
                T += delta / H * self.dt * weight
                T_ += (delta / H * self.dt + theta) * weight
            self.T[p] += T
            self.T_[p] = T_

    def step(self, current_t):
        self.grid.deactivate_all()
        self.build_pid()
        print('step0')
        self.step0()
        print('step1')
        self.step1()
        print('step2')
        self.step2(1. if current_t < 0.002 else 0.)
        print('step3')
        self.step3()
        print('step4')

    @ti.kernel
    def visualize(self):
        # for I in ti.grouped(self.grid_H):
        #     T = self.grid_delta[I] / self.grid_H[I] * self.dt + self.grid_theta[I]
        for I in ti.grouped(self.grid_theta):
            T = self.grid_theta[I] / 1000.
            i = I[0] + self.res // 2
            j = I[1] + self.res // 2
            if 0 <= i < self.res and 0 <= j < self.res:
                ti.atomic_max(self.img[i, j], T)

ti.init(arch=ti.gpu, default_fp=real)

mpm = MPMSolver()
mpm.sample_particles()
gui = ti.GUI("ExaAM")
current_t = 0.
while current_t < 0.0045:
    print("current_t", current_t)
    mpm.step(current_t)
    mpm.img.fill(0)
    mpm.visualize()
    gui.set_image(mpm.img)
    gui.show(f'outputs/{int(current_t / mpm.dt):06d}.png' )
    current_t += mpm.dt