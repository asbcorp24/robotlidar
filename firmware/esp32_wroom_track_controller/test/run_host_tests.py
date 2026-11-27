"""Run actual relay/MCP23017 code against a register-level I2C test double."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
compiler = shutil.which('g++')
if not compiler:
    raise SystemExit('g++ is required to run host tests')
output = ROOT / '.pio/host-tests'
output.mkdir(parents=True, exist_ok=True)
for suite in ('drive_relays_test', 'brush_relays_test', 'a4988_stepper_test'):
    binary = output / (suite + '.exe')
    subprocess.run([
        compiler, '-std=c++14', '-Wall', '-Wextra', '-Werror',
        '-I' + str(ROOT / 'test/host/stubs'), '-I' + str(ROOT / 'include'),
        str(ROOT / ('test/host/' + suite + '.cpp')),
        str(ROOT / 'src/drive_relays.cpp'), str(ROOT / 'src/mcp23017_controller.cpp'),
        '-o', str(binary),
    ], check=True)
    subprocess.run([str(binary)], check=True)
