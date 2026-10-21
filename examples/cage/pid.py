# A fixed-point PID controller driving one robot joint toward a target angle.
#
# Values are Q15 fixed point: an int16 x stands for x / 32768, so the range
# is [-1, 1). q15mul is the hot function, and the one an AI agent is asked
# to turn into a custom instruction.

def q15mul(a: int16, b: int16) -> int16:
    # Multiply, round to nearest, and saturate: -1 * -1 does not fit.
    p = int(a) * int(b)
    r = (p + 16384) >> 15
    return int16(min(max(r, -32768), 32767))

def sat16(x: int) -> int16:
    return int16(min(max(x, -32768), 32767))

KP: int16 = 9830
KI: int16 = 164
KD: int16 = 16384

def control(err: int16, integ: int16, deriv: int16) -> int16:
    u = int(q15mul(KP, err)) + int(q15mul(KI, integ)) + int(q15mul(KD, deriv))
    return sat16(u)

target: int16 = 16384
angle: int16 = 0
vel: int16 = 0
integ: int16 = 0
prev: int16 = 0
for t in range(2000):
    err = sat16(int(target) - int(angle))
    integ = sat16(int(integ) + (int(err) >> 4))
    deriv = sat16(int(err) - int(prev))
    prev = err
    u = control(err, integ, deriv)
    # The joint: torque accelerates it, friction slows it down.
    vel = sat16(int(vel) + (int(u) >> 4) - (int(vel) >> 3))
    angle = sat16(int(angle) + (int(vel) >> 3))
    if t % 250 == 0:
        print(t, angle, u)
print("final", angle)
