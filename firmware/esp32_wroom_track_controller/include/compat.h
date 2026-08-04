#pragma once

#include <algorithm>

// Arduino defines max(a, b) as a function-like macro. The firmware uses an
// explicitly typed max<uint32_t>(), which is not expanded by that macro.
// Bring std::max into global lookup for that expression.
using std::max;
