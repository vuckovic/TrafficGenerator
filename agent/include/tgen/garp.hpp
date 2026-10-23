// SPDX-License-Identifier: MIT
#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace tgen {

// Sends gratuitous ARP (request and reply form) for each address on iface, `repeat`
// rounds 200 ms apart. Returns frames sent, or -1 with err set. Needs CAP_NET_RAW.
int send_garp(const std::string& iface, const std::vector<std::uint32_t>& ips_host_order, std::string& err,
              int repeat = 2);

}  // namespace tgen
