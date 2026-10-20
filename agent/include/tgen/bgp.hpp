// SPDX-License-Identifier: MIT
// TrafficGenerator - BGP-4 message model, encoder and decoder (RFC 4271).
// Supports: OPEN with capabilities (MP-BGP RFC 4760, route refresh RFC 2918,
// 4-octet AS RFC 6793), UPDATE (IPv4 NLRI / withdrawals; ORIGIN, AS_PATH,
// AS4_PATH, NEXT_HOP, MED, LOCAL_PREF, COMMUNITIES RFC 1997, ORIGINATOR_ID,
// CLUSTER_LIST, LARGE_COMMUNITY RFC 8092), KEEPALIVE, NOTIFICATION.
// The encoder is shared by the stateful router emulator and the stateless
// line-rate generator, so both put identical bytes on the wire.
#pragma once

#include <array>
#include <bitset>
#include <cstdint>
#include <optional>
#include <span>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace tgen::bgp {

using Bytes = std::vector<std::uint8_t>;

inline constexpr std::size_t kHeaderLen = 19;
inline constexpr std::size_t kMaxMessageLen = 4096;
inline constexpr std::uint32_t kAsTrans = 23456;

enum class MsgType : std::uint8_t { Open = 1, Update = 2, Notification = 3, Keepalive = 4, RouteRefresh = 5 };

enum class Origin : std::uint8_t { Igp = 0, Egp = 1, Incomplete = 2 };

namespace attr {
inline constexpr std::uint8_t kOrigin = 1, kAsPath = 2, kNextHop = 3, kMed = 4, kLocalPref = 5,
                              kCommunities = 8, kOriginatorId = 9, kClusterList = 10,
                              kAs4Path = 17, kLargeCommunity = 32;
}

class DecodeError : public std::runtime_error {
 public:
  DecodeError(std::uint8_t code, std::uint8_t subcode, const std::string& what)
      : std::runtime_error(what), code_(code), subcode_(subcode) {}
  std::uint8_t code() const { return code_; }
  std::uint8_t subcode() const { return subcode_; }

 private:
  std::uint8_t code_, subcode_;
};

// ---------------------------------------------------------------- prefix ---
struct Prefix {
  std::uint32_t addr = 0;  // host byte order, host bits zeroed
  std::uint8_t len = 0;    // 0..32

  static Prefix make(std::uint32_t addr, std::uint8_t len);  // masks host bits
  static Prefix parse(std::string_view cidr);                 // "10.1.2.0/24"
  std::string str() const;
  std::uint64_t key() const { return (static_cast<std::uint64_t>(addr) << 8) | len; }
  friend bool operator==(const Prefix&, const Prefix&) = default;
};

std::uint32_t parse_ipv4(std::string_view s);
std::string format_ipv4(std::uint32_t a);

// ------------------------------------------------------------ attributes ---
struct LargeCommunity {
  std::uint32_t global = 0, local1 = 0, local2 = 0;
  friend bool operator==(const LargeCommunity&, const LargeCommunity&) = default;
};

struct PathAttributes {
  Origin origin = Origin::Igp;
  std::vector<std::uint32_t> as_path;  // AS_SEQUENCE, leftmost = neighbour
  std::uint32_t next_hop = 0;          // host order
  std::optional<std::uint32_t> med;
  std::optional<std::uint32_t> local_pref;  // iBGP only
  std::vector<std::uint32_t> communities;
  std::vector<LargeCommunity> large_communities;
  std::optional<std::uint32_t> originator_id;  // decoded only (route reflector)
  std::vector<std::uint32_t> cluster_list;     // decoded only (route reflector)
  bool has_next_hop = false;                   // decoded only
  friend bool operator==(const PathAttributes&, const PathAttributes&) = default;
};

// --------------------------------------------------------------- messages ---
struct Capabilities {
  bool mp_ipv4_unicast = true;
  bool route_refresh = true;
  bool four_octet_as = true;
  std::uint32_t asn = 0;  // value of the 4-octet AS capability
};

struct OpenMsg {
  std::uint8_t version = 4;
  std::uint32_t asn = 0;      // real ASN (from the 4-octet capability if present)
  std::uint16_t my_as16 = 0;  // as carried in the fixed field
  std::uint16_t hold_time = 90;
  std::uint32_t router_id = 0;
  Capabilities caps;
  bool caps_present = false;
};

struct UpdateMsg {
  std::vector<Prefix> withdrawn;
  PathAttributes attrs;
  bool has_attrs = false;
  std::bitset<256> attr_types;  // every path attribute type code present (decoded only)
  std::vector<Prefix> nlri;
};

struct NotificationMsg {
  std::uint8_t code = 0, subcode = 0;
  Bytes data;
};

// --------------------------------------------------------------- encoder ---
Bytes encode_open(std::uint32_t asn, std::uint16_t hold_time, std::uint32_t router_id,
                  const Capabilities& caps);
Bytes encode_keepalive();
Bytes encode_notification(std::uint8_t code, std::uint8_t subcode, std::span<const std::uint8_t> data = {});

// One UPDATE. `as4` = both sides negotiated 4-octet AS; if false, ASNs > 65535
// are written as AS_TRANS in AS_PATH and the real path goes into AS4_PATH.
// Throws std::length_error if it does not fit into 4096 bytes.
Bytes encode_update(std::span<const Prefix> withdrawn, const PathAttributes* attrs,
                    std::span<const Prefix> nlri, bool as4);

// Packs announcements sharing `attrs` into as few UPDATEs as possible, at most
// `max_per_update` prefixes each (0 = as many as fit into 4096 bytes).
std::vector<Bytes> build_announcements(std::span<const Prefix> nlri, const PathAttributes& attrs,
                                       bool as4, std::size_t max_per_update = 0);
std::vector<Bytes> build_withdrawals(std::span<const Prefix> withdrawn, std::size_t max_per_update = 0);

// Size helpers (exact on-wire size, used for packing)
std::size_t nlri_size(const Prefix& p);
std::size_t attrs_size(const PathAttributes& a, bool as4);

// --------------------------------------------------------------- decoder ---
struct Message {
  MsgType type{};
  Bytes body;  // without the 19-byte header
};

// Incremental parser for a TCP byte stream; validates the header.
class StreamParser {
 public:
  void feed(std::span<const std::uint8_t> data);
  // Returns the next complete message or nullopt. Throws DecodeError
  // (code 1 = message header error) on malformed headers.
  std::optional<Message> next();
  std::size_t buffered() const { return buf_.size() - pos_; }

 private:
  Bytes buf_;
  std::size_t pos_ = 0;
};

OpenMsg decode_open(std::span<const std::uint8_t> body);
// `as4` = 4-octet AS negotiated. Without it, AS_PATH and AS4_PATH are merged
// into attrs.as_path as RFC 6793 §4.2.3 describes.
UpdateMsg decode_update(std::span<const std::uint8_t> body, bool as4);
NotificationMsg decode_notification(std::span<const std::uint8_t> body);

std::string notification_text(std::uint8_t code, std::uint8_t subcode);

}  // namespace tgen::bgp
