import taichi as ti
import numpy as np
import sys
from common.utils.particleSampling import *
from common.utils.cfl import *
from projects.brittle.DFGMPMSolver import *
from projects.brittle.ExplicitMPMSolver import *

ti.init(default_fp=ti.f64, arch=ti.gpu) # Try to run on GPU    #GPU, parallel
#ti.init(default_fp=ti.f64, arch=ti.cpu, cpu_max_num_threads=1)  #CPU, sequential

gravity = 0.0
outputPath = "../output/mode1Fracture/brittle.ply"
outputPath2 = "../output/mode1Fracture/brittle_nodes.ply"
fps = 24
endFrame = 1#0 * fps

E, nu = 1000, 0.2 #TODO
EList = [E]
nuList = [nu]

st = 10  #TODO
surfaceThresholds = [st]

maxArea = 'qa0.0000025'

grippedMaterial = 0.005
minPoint = [0.4, 0.4 - grippedMaterial]
maxPoint = [0.6, 0.6 + grippedMaterial]
vertices = sampleNotchedBox2D(minPoint, maxPoint, maxArea)
vertexCount = len(vertices)
particleCounts = [vertexCount]

rho = 8 #TODO
vol = 0.2 * (0.2 + (grippedMaterial*2))
pVol = vol / vertexCount
mp = pVol * rho
particleMasses = [mp]
particleVolumes = [pVol]

initVel = [0,0]
initialVelocity = [initVel]

#dx = 0.01 #TODO
ppc = 8
dx = (ppc * pVol)**0.5

#Compute max dt
cfl = 0.4
maxDt = suggestedDt(E, nu, rho, dx, cfl)
dt = 0.9 * maxDt

useFrictionalContact = True
verbose = False
useAPIC = False
frictionCoefficient = 0.0
flipPicRatio = 0.95

if(len(sys.argv) == 6):
    outputPath = sys.argv[4]
    outputPath2 = sys.argv[5]

solver = DFGMPMSolver(endFrame, fps, dt, dx, EList, nuList, gravity, cfl, ppc, vertices, particleCounts, particleMasses, particleVolumes, initialVelocity, outputPath, outputPath2, surfaceThresholds, useFrictionalContact, frictionCoefficient, verbose, useAPIC, flipPicRatio)
#solver = ExplicitMPMSolver(endFrame, fps, dt, dx, EList, nuList, gravity, cfl, ppc, vertices, particleCounts, particleMasses, particleVolumes, initialVelocity, outputPath, outputPath2, verbose, useAPIC, flipPicRatio)

#Add Damage Model
Gf = 5 #TODO
sigmaF = 100 #TODO
dMin = 0.5 #TODO, this controls how much damage must accumulate before we allow a node to separate

if(len(sys.argv) == 6):
    Gf = sys.argv[1]
    sigmaF = sys.argv[2]
    dMin = sys.argv[3]

damageList = [1]
solver.addRankineDamage(damageList, Gf, sigmaF, E, dMin)

#Collision Objects
lowerCenter = (0.0, minPoint[1] + grippedMaterial)
lowerNormal = (0, 1)
upperCenter = (0.0, maxPoint[1] - grippedMaterial)
upperNormal = (0, -1)

def lowerTransform(time: ti.f64):
    translation = [0.0,0.0]
    velocity = [0.0,0.0]
    startTime = 0.0
    endTime = 2.0
    speed = -0.005
    if time >= startTime:
        translation = [0.0, speed * (time-startTime)]
        velocity = [0.0, speed]
    if time > endTime:
        translation = [0.0, 0.0]
        velocity = [0.0, 0.0]
    return translation, velocity

def upperTransform(time: ti.f64):
    translation = [0.0,0.0]
    velocity = [0.0,0.0]
    startTime = 0.0
    endTime = 2.0
    speed = 0.005
    if time >= startTime:
        translation = [0.0, speed * (time-startTime)]
        velocity = [0.0, speed]
    if time > endTime:
        translation = [0.0, 0.0]
        velocity = [0.0, 0.0]
    return translation, velocity

solver.addHalfSpace(lowerCenter, lowerNormal, surface = solver.surfaceSticky, friction = 0.0, transform = lowerTransform)
solver.addHalfSpace(upperCenter, upperNormal, surface = solver.surfaceSticky, friction = 0.0, transform = upperTransform)

#Simulate!
solver.simulate()