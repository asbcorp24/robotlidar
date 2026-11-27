#pragma once
#include <array>
#include <vector>
#include <stdint.h>

struct RegisterWrite { uint8_t address, value; };
class TwoWire {
public:
    std::array<uint8_t,256> regs{};
    std::vector<RegisterWrite> writes;
    std::vector<uint8_t> tx;
    std::vector<std::vector<uint8_t>> dacFrames;
    int transactions = 0, failAt = -1;
    uint8_t pointer = 0, device = 0;
    void reset() {
        regs.fill(0); regs[0] = regs[1] = 0xFF;
        writes.clear(); tx.clear(); dacFrames.clear(); transactions = 0; failAt = -1;
    }
    void beginTransmission(uint8_t address) { device=address;tx.clear(); }
    void write(uint8_t value) { tx.push_back(value); }
    int endTransmission(bool = true) {
        if (++transactions == failAt) return 2;
        if (device != 0x20) {
            if (!tx.empty()) dacFrames.push_back(tx);
            return 0;
        }
        if (!tx.empty()) pointer = tx[0];
        if (tx.size() == 2) {
            regs[tx[0]] = tx[1]; writes.push_back({tx[0],tx[1]});
        }
        return 0;
    }
    int requestFrom(int,int count) { return count; }
    int read() { return regs[pointer++]; }
};
