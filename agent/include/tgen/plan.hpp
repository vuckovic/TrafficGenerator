// SPDX-License-Identifier: MIT
// Per-node run plan (JSON in) and result (JSON out) for tgen-agent.
#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "tgen/bgp.hpp"

namespace tgen {

enum class ReceiveMode { Full, Timestamps, Count };
enum class DutRole { RouteServer, RouteReflector, Router };

struct RouterPlan {
  std::string name;
  std::uint32_t asn = 0;
  std::uint32_t ip = 0;         // local address (host order), must exist on the node
  std::uint32_t router_id = 0;  // defaults to ip
  std::vector<bgp::Prefix> prefixes;
  bgp::PathAttributes attrs;    // as_path = [asn, ...extra]; next_hop = ip unless set
  ReceiveMode receive = ReceiveMode::Timestamps;
  std::uint64_t expected_rx = 0;  // routes this router should end up holding
};

struct Timing {             // absolute times, ms since the Unix epoch (chrony-synced); CLI mode only
  bool present = false;     // serve mode: phases arrive over the API instead
  std::int64_t establish_at = 0;
  std::int64_t announce_at = 0;
  std::optional<std::int64_t> withdraw_at;
  std::int64_t end_at = 0;
  double establish_per_s = 0;  // stagger session starts; 0 = all at once
};

struct Plan {
  std::string run_id;
  std::string node;
  std::uint32_t dut_ip = 0;
  std::uint16_t dut_port = 179;
  std::uint32_t dut_asn = 0;
  DutRole role = DutRole::RouteServer;
  std::uint16_t hold_time = 90;
  int connect_retry_s = 5;
  double announce_rate = 0;       // prefixes/s per router; 0 = as fast as the session allows
  std::size_t per_update = 0;     // max prefixes per UPDATE; 0 = fill 4096 bytes
  std::uint32_t sample_mod = 100; // latency sample: prefixes with hash % sample_mod == 0
  std::uint32_t live_sample_mod = 1000;  // subset reported live (serve mode stats)
  std::uint64_t seed = 1;         // churn / flap randomness (reproducible)
  std::string iface;              // serve mode: interface for the routers' /32 addresses ("" = do not manage)
  double controller_timeout_s = 5;  // serve mode: stop the run without controller contact for this long
  std::vector<int> cores;         // worker threads pinned to these cores (empty = no pinning)
  Timing timing;
  std::vector<RouterPlan> routers;
};

Plan plan_from_json(const nlohmann::json& j);  // throws std::invalid_argument with a clear message

inline std::uint64_t mix64(std::uint64_t x) {  // splitmix64 finaliser
  x += 0x9E3779B97F4A7C15ull;
  x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ull;
  x = (x ^ (x >> 27)) * 0x94D049BB133111EBull;
  return x ^ (x >> 31);
}
inline bool sampled(const bgp::Prefix& p, std::uint32_t mod) { return mod != 0 && mix64(p.key()) % mod == 0; }

}  // namespace tgen
