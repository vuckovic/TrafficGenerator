// SPDX-License-Identifier: MIT
#include <gtest/gtest.h>

#include "tgen/plan.hpp"

using nlohmann::json;
using namespace tgen;

namespace {
json base() {
  return json::parse(R"({
    "run_id": "t1", "node": "CMNODE02",
    "dut": {"ip": "10.100.0.16", "asn": 64500, "role": "route_server"},
    "timing": {"establish_at": 1000, "announce_at": 2000, "end_at": 9000},
    "routers": [{"name": "R01", "asn": 4200000117, "ip": "10.100.0.20",
                 "prefixes": ["10.0.0.0/24", "10.0.1.0/24"],
                 "attrs": {"as_path_extra": [64700], "med": 10, "large_communities": [[64500, 1, 2]]}}]
  })");
}
}  // namespace

TEST(Plan, ParsesRouteServerPlan) {
  Plan p = plan_from_json(base());
  ASSERT_EQ(p.routers.size(), 1u);
  const auto& r = p.routers[0];
  EXPECT_EQ(r.asn, 4200000117u);
  EXPECT_EQ(r.attrs.as_path, (std::vector<std::uint32_t>{4200000117u, 64700}));
  EXPECT_EQ(bgp::format_ipv4(r.attrs.next_hop), "10.100.0.20");
  EXPECT_EQ(r.attrs.med, 10u);
  EXPECT_FALSE(r.attrs.local_pref.has_value());
  EXPECT_EQ(r.prefixes.size(), 2u);
}

TEST(Plan, RouteReflectorPutsAsnAsOrigin) {
  json j = base();
  j["dut"]["role"] = "route_reflector";
  Plan p = plan_from_json(j);
  EXPECT_EQ(p.routers[0].attrs.as_path, (std::vector<std::uint32_t>{64700, 4200000117u}));
  EXPECT_EQ(p.routers[0].attrs.local_pref, 100u);
}

TEST(Plan, RejectsBadInput) {
  json j = base();
  j["timing"]["announce_at"] = 500;  // before establish
  EXPECT_THROW(plan_from_json(j), std::invalid_argument);
  j = base();
  j["routers"].push_back(j["routers"][0]);  // duplicate ip
  EXPECT_THROW(plan_from_json(j), std::invalid_argument);
  j = base();
  j["routers"][0].erase("asn");
  EXPECT_THROW(plan_from_json(j), std::invalid_argument);
  j = base();
  j["hold_time"] = 2;
  EXPECT_THROW(plan_from_json(j), std::invalid_argument);
}

TEST(Plan, SamplingIsDeterministicAndRoughlyOnePercent) {
  int hits = 0;
  for (std::uint32_t i = 0; i < 100000; ++i) hits += sampled(bgp::Prefix::make(0x0A000000u + (i << 4), 28), 100);
  EXPECT_GT(hits, 800);
  EXPECT_LT(hits, 1200);
  auto p = bgp::Prefix::parse("10.1.2.0/24");
  EXPECT_EQ(sampled(p, 100), sampled(p, 100));
}
