// SPDX-License-Identifier: MIT
#include "tgen/plan.hpp"

#include <stdexcept>
#include <unordered_set>

namespace tgen {

using nlohmann::json;

namespace {

template <typename T>
T req(const json& j, const char* key, const std::string& where) {
  if (!j.contains(key)) throw std::invalid_argument(where + ": missing \"" + key + "\"");
  try {
    return j.at(key).get<T>();
  } catch (const json::exception& e) {
    throw std::invalid_argument(where + ": bad \"" + key + "\": " + e.what());
  }
}

bgp::Origin origin_from(const std::string& s) {
  if (s == "igp") return bgp::Origin::Igp;
  if (s == "egp") return bgp::Origin::Egp;
  if (s == "incomplete") return bgp::Origin::Incomplete;
  throw std::invalid_argument("bad origin: " + s);
}

}  // namespace

Plan plan_from_json(const json& j) {
  Plan p;
  p.run_id = j.value("run_id", "");
  p.node = j.value("node", "");
  const json& dut = j.at("dut");
  p.dut_ip = bgp::parse_ipv4(req<std::string>(dut, "ip", "dut"));
  p.dut_asn = req<std::uint32_t>(dut, "asn", "dut");
  p.dut_port = dut.value("port", 179);
  const std::string role = dut.value("role", "route_server");
  if (role == "route_server") p.role = DutRole::RouteServer;
  else if (role == "route_reflector") p.role = DutRole::RouteReflector;
  else if (role == "router") p.role = DutRole::Router;
  else throw std::invalid_argument("dut.role: " + role);

  p.hold_time = j.value("hold_time", 90);
  if (p.hold_time == 1 || p.hold_time == 2) throw std::invalid_argument("hold_time must be 0 or >= 3");
  p.connect_retry_s = j.value("connect_retry_s", 5);
  p.announce_rate = j.value("announce_rate", 0.0);
  p.per_update = j.value("per_update", 0);
  p.sample_mod = j.value("sample_mod", 100);
  p.cores = j.value("cores", std::vector<int>{});

  const json& t = j.at("timing");
  p.timing.establish_at = req<std::int64_t>(t, "establish_at", "timing");
  p.timing.announce_at = req<std::int64_t>(t, "announce_at", "timing");
  if (t.contains("withdraw_at") && !t["withdraw_at"].is_null()) p.timing.withdraw_at = t["withdraw_at"].get<std::int64_t>();
  p.timing.end_at = req<std::int64_t>(t, "end_at", "timing");
  p.timing.establish_per_s = t.value("establish_per_s", 0.0);
  if (!(p.timing.establish_at <= p.timing.announce_at && p.timing.announce_at < p.timing.end_at))
    throw std::invalid_argument("timing: need establish_at <= announce_at < end_at");

  std::unordered_set<std::uint32_t> ips;
  for (const json& r : j.at("routers")) {
    RouterPlan rp;
    rp.name = req<std::string>(r, "name", "router");
    const std::string where = "router " + rp.name;
    rp.asn = req<std::uint32_t>(r, "asn", where);
    rp.ip = bgp::parse_ipv4(req<std::string>(r, "ip", where));
    if (!ips.insert(rp.ip).second) throw std::invalid_argument(where + ": duplicate ip");
    rp.router_id = r.contains("router_id") ? bgp::parse_ipv4(r["router_id"].get<std::string>()) : rp.ip;
    for (const auto& s : r.value("prefixes", std::vector<std::string>{})) rp.prefixes.push_back(bgp::Prefix::parse(s));
    const std::string rx = r.value("receive", "timestamps");
    rp.receive = rx == "full" ? ReceiveMode::Full : rx == "count" ? ReceiveMode::Count : ReceiveMode::Timestamps;
    rp.expected_rx = r.value("expected_rx", 0ull);

    const json a = r.value("attrs", json::object());
    rp.attrs.origin = origin_from(a.value("origin", "igp"));
    // eBGP (route server, router): [own ASN, extra...]. iBGP to a route
    // reflector: the session runs in the DUT's AS and the router's ASN is the
    // origin AS of its routes: [extra..., ASN].
    const auto extra = a.value("as_path_extra", std::vector<std::uint32_t>{});
    if (p.role != DutRole::RouteReflector) rp.attrs.as_path.push_back(rp.asn);
    rp.attrs.as_path.insert(rp.attrs.as_path.end(), extra.begin(), extra.end());
    if (p.role == DutRole::RouteReflector) rp.attrs.as_path.push_back(rp.asn);
    rp.attrs.next_hop = a.contains("next_hop") ? bgp::parse_ipv4(a["next_hop"].get<std::string>()) : rp.ip;
    if (a.contains("med") && !a["med"].is_null()) rp.attrs.med = a["med"].get<std::uint32_t>();
    if (p.role == DutRole::RouteReflector) rp.attrs.local_pref = a.value("local_pref", 100u);
    rp.attrs.communities = a.value("communities", std::vector<std::uint32_t>{});
    for (const auto& lc : a.value("large_communities", std::vector<std::vector<std::uint32_t>>{})) {
      if (lc.size() != 3) throw std::invalid_argument(where + ": large community needs 3 numbers");
      rp.attrs.large_communities.push_back({lc[0], lc[1], lc[2]});
    }
    p.routers.push_back(std::move(rp));
  }
  if (p.routers.empty()) throw std::invalid_argument("plan has no routers");
  return p;
}

}  // namespace tgen
