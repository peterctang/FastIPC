import os
import numpy as np
import taichi as ti

from projects.mpm.engine.mpm_solver_implicit import MPMSolverImplicit

ti.init(arch=ti.cpu, default_fp=ti.f64)

directory = 'output/'
os.makedirs(directory, exist_ok=True)

# gui = ti.GUI("Taichi Elements", res=512, background_color=0x112F41)

# mpm = MPMSolverImplicit(res=(128,128))

# mpm.add_cube(pos=(0.4, 0.5), size=(0.2, 0.2))

# mpm.add_surface_collider(point=(0.0, 0.02), normal=(0.0, 1.0))

# mpm.add_analytic_box()

# mpm.test()


gui = ti.GUI("Taichi Elements", res=512, background_color=0x112F41)

mpm = MPMSolverImplicit(res=(128,128))

mpm.dt = 0.0001

mpm.add_cube(pos=(0.3, 0.7), size=(0.4, 0.2))

mpm.add_surface_collider(point=(0.0, 0.02), normal=(0.0, 1.0))

# mpm.add_analytic_box()


for frame in range(100000):
    # print(frame)
    mpm.step(8e-3)
    particles = mpm.particle_info()
    gui.circles(particles['position'],
                radius=1.5,
                color=0x068587)
    gui.show(directory + f'{frame:06d}.png')
    # gui.show()



# gui = ti.GUI("Taichi Elements", res=512, background_color=0x112F41)
# mpm = MPMSolverImplicit(res=(64, 64, 64), size=10)

# mpm.add_cube(pos=(2, 6, 3), size=(1, 1, 3))

# mpm.add_surface_collider(point=(0.0, 0.05, 0.0), normal=(0.0, 1.0, 0.0))

# for frame in range(1500):
#     mpm.step(4e-3)
#     colors = np.array([0x068587, 0xED553B, 0xEEEEF0, 0xFFFF00],
#                       dtype=np.uint32)
#     particles = mpm.particle_info()
#     np_x = particles['position'] / 10.0

#     # simple camera transform
#     screen_x = ((np_x[:, 0] + np_x[:, 2]) / 2**0.5) - 0.2
#     screen_y = (np_x[:, 1])

#     screen_pos = np.stack([screen_x, screen_y], axis=-1)

#     gui.circles(screen_pos, radius=1.5, color=0x068587)
#     gui.show(f'{frame:06d}.png')