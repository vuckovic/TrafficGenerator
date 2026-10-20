// SPDX-License-Identifier: MIT
#include "tgen/bgp.hpp"

#include <algorithm>
#include <charconv>
#include <cstring>

namespace tgen::bgp {

namespace {

void put8(Bytes& b, std::uint8_t v) { b.push_back(v); }
void put16(Bytes& b, std::uint16_t v) {
  b.push_back(static_cast<std::uint8_t>(v >> 8));
  b.push_back(static_cast<std::uint8_t>(v));
}
void put32(Bytes& b, std::uint32_t v) {
  put16(b, static_cast<std::uint16_t>(v >> 16));
  put16(b, static_cast<std::uint16_t>(v));
}

class Reader {
 public:
  explicit Reader(std::span<const std::uint8_t> d, std::uint8_t code = 1, std::uint8_t sub = 0)
      : d_(d), code_(code), sub_(sub) {}
  std::size_t left() const { return d_.size() - p_; }
  bool done() const { return p_ >= d_.size(); }
  void need(std::size_t n) const {
    if (left() < n) throw DecodeError(code_, sub_, "truncated message");
  }
  std::uint8_t u8() { need(1); return d_[p_++]; }
  std::uint16_t u16() { need(2); std::uint16_t v = (d_[p_] << 8) | d_[p_ + 1]; p_ += 2; return v; }
  std::uint32_t u32() { std::uint32_t hi = u16(); return (hi << 16) | u16(); }
  std::span<const std::uint8_t> take(std::size_t n) { need(n); auto s = d_.subspan(p_, n); p_ += n; return s; }

 private:
  std::span<const std::uint8_t> d_;
  std::size_t p_ = 0;
  std::uint8_t code_, sub_;
};

Bytes with_header(MsgType t, const Bytes& body) {
  Bytes m;
  m.reserve(kHeaderLen + body.size());
  m.insert(m.end(), 16, 0xFF);
  put16(m, static_cast<std::uint16_t>(kHeaderLen + body.size()));
  put8(m, static_cast<std::uint8_t>(t));
  m.insert(m.end(), body.begin(), body.end());
  return m;
}

void put_prefix(Bytes& b, const Prefix& p) {
  put8(b, p.len);
  const std::size_t n = (p.len + 7) / 8;
  for (std::size_t i = 0; i < n; ++i) b.push_back(static_cast<std::uint8_t>(p.addr >> (24 - 8 * i)));
}

Prefix read_prefix(Reader& r) {
  const std::uint8_t len = r.u8();
  if (len > 32) throw DecodeError(3, 10, "invalid prefix length");
  const std::size_t n = (len + 7) / 8;
  auto bytes = r.take(n);
  std::uint32_t a = 0;
  for (std::size_t i = 0; i < n; ++i) a |= static_cast<std::uint32_t>(bytes[i]) << (24 - 8 * i);
  return Prefix::make(a, len);
}

void put_attr(Bytes& b, std::uint8_t flags, std::uint8_t type, const Bytes& val) {
  if (val.size() > 255) flags |= 0x10;  // extended length
  put8(b, flags);
  put8(b, type);
  if (flags & 0x10) put16(b, static_cast<std::uint16_t>(val.size()));
  else put8(b, static_cast<std::uint8_t>(val.size()));
  b.insert(b.end(), val.begin(), val.end());
}

Bytes as_path_value(const std::vector<std::uint32_t>& path, bool four, bool trans) {
  Bytes v;
  for (std::size_t i = 0; i < path.size(); i += 255) {
    const std::size_t n = std::min<std::size_t>(255, path.size() - i);
    put8(v, 2);  // AS_SEQUENCE
    put8(v, static_cast<std::uint8_t>(n));
    for (std::size_t j = 0; j < n; ++j) {
      const std::uint32_t as = path[i + j];
      if (four) put32(v, as);
      else put16(v, static_cast<std::uint16_t>(trans && as > 0xFFFF ? kAsTrans : as));
    }
  }
  return v;
}

std::vector<std::uint32_t> read_as_path(std::span<const std::uint8_t> v, bool four) {
  Reader r(v, 3, 11);
  std::vector<std::uint32_t> out;
  const std::size_t w = four ? 4 : 2;
  while (!r.done()) {
    const std::uint8_t seg_type = r.u8();
    const std::uint8_t n = r.u8();
    if (seg_type < 1 || seg_type > 4) throw DecodeError(3, 11, "bad AS_PATH segment type");
    r.need(n * w);
    for (std::uint8_t i = 0; i < n; ++i) out.push_back(four ? r.u32() : r.u16());
  }
  return out;
}

Bytes encode_attrs(const PathAttributes& a, bool as4) {
  Bytes b;
  put_attr(b, 0x40, attr::kOrigin, Bytes{static_cast<std::uint8_t>(a.origin)});
  const bool needs_as4_path =
      !as4 && std::any_of(a.as_path.begin(), a.as_path.end(), [](std::uint32_t x) { return x > 0xFFFF; });
  put_attr(b, 0x40, attr::kAsPath, as_path_value(a.as_path, as4, true));
  { Bytes v; put32(v, a.next_hop); put_attr(b, 0x40, attr::kNextHop, v); }
  if (a.med) { Bytes v; put32(v, *a.med); put_attr(b, 0x80, attr::kMed, v); }
  if (a.local_pref) { Bytes v; put32(v, *a.local_pref); put_attr(b, 0x40, attr::kLocalPref, v); }
  if (!a.communities.empty()) {
    Bytes v;
    for (auto c : a.communities) put32(v, c);
    put_attr(b, 0xC0, attr::kCommunities, v);
  }
  if (needs_as4_path) put_attr(b, 0xC0, attr::kAs4Path, as_path_value(a.as_path, true, false));
  if (!a.large_communities.empty()) {
    Bytes v;
    for (const auto& c : a.large_communities) { put32(v, c.global); put32(v, c.local1); put32(v, c.local2); }
    put_attr(b, 0xC0, attr::kLargeCommunity, v);
  }
  return b;
}

}  // namespace

// ---------------------------------------------------------------- prefix ---
Prefix Prefix::make(std::uint32_t addr, std::uint8_t len) {
  if (len > 32) throw std::invalid_argument("prefix length > 32");
  const std::uint32_t mask = len == 0 ? 0 : (len == 32 ? 0xFFFFFFFFu : ~(0xFFFFFFFFu >> len));
  return Prefix{addr & mask, len};
}

std::uint32_t parse_ipv4(std::string_view s) {
  std::uint32_t a = 0;
  int parts = 0;
  const char* p = s.data();
  const char* e = s.data() + s.size();
  while (p < e) {
    unsigned v = 0;
    auto [q, ec] = std::from_chars(p, e, v);
    if (ec != std::errc() || v > 255 || q == p) throw std::invalid_argument("bad IPv4: " + std::string(s));
    a = (a << 8) | v;
    ++parts;
    p = q;
    if (p < e) {
      if (*p != '.') throw std::invalid_argument("bad IPv4: " + std::string(s));
      ++p;
    }
  }
  if (parts != 4) throw std::invalid_argument("bad IPv4: " + std::string(s));
  return a;
}

std::string format_ipv4(std::uint32_t a) {
  return std::to_string(a >> 24) + "." + std::to_string((a >> 16) & 255) + "." + std::to_string((a >> 8) & 255) +
         "." + std::to_string(a & 255);
}

Prefix Prefix::parse(std::string_view cidr) {
  const auto slash = cidr.find('/');
  if (slash == std::string_view::npos) throw std::invalid_argument("missing /len: " + std::string(cidr));
  unsigned len = 0;
  auto lens = cidr.substr(slash + 1);
  auto [q, ec] = std::from_chars(lens.data(), lens.data() + lens.size(), len);
  if (ec != std::errc() || q != lens.data() + lens.size() || len > 32)
    throw std::invalid_argument("bad prefix length: " + std::string(cidr));
  return make(parse_ipv4(cidr.substr(0, slash)), static_cast<std::uint8_t>(len));
}

std::string Prefix::str() const { return format_ipv4(addr) + "/" + std::to_string(len); }

// --------------------------------------------------------------- encoder ---
Bytes encode_open(std::uint32_t asn, std::uint16_t hold_time, std::uint32_t router_id, const Capabilities& caps) {
  Bytes c;  // capabilities
  if (caps.mp_ipv4_unicast) { put8(c, 1); put8(c, 4); put16(c, 1); put8(c, 0); put8(c, 1); }
  if (caps.route_refresh) { put8(c, 2); put8(c, 0); }
  if (caps.four_octet_as) { put8(c, 65); put8(c, 4); put32(c, asn); }
  Bytes b;
  put8(b, 4);
  put16(b, static_cast<std::uint16_t>(asn > 0xFFFF ? kAsTrans : asn));
  put16(b, hold_time);
  put32(b, router_id);
  if (c.empty()) {
    put8(b, 0);
  } else {
    put8(b, static_cast<std::uint8_t>(c.size() + 2));
    put8(b, 2);  // optional parameter: capabilities
    put8(b, static_cast<std::uint8_t>(c.size()));
    b.insert(b.end(), c.begin(), c.end());
  }
  return with_header(MsgType::Open, b);
}

Bytes encode_keepalive() { return with_header(MsgType::Keepalive, {}); }

Bytes encode_notification(std::uint8_t code, std::uint8_t subcode, std::span<const std::uint8_t> data) {
  Bytes b{code, subcode};
  b.insert(b.end(), data.begin(), data.end());
  return with_header(MsgType::Notification, b);
}

std::size_t nlri_size(const Prefix& p) { return 1 + (p.len + 7) / 8; }
std::size_t attrs_size(const PathAttributes& a, bool as4) { return encode_attrs(a, as4).size(); }

Bytes encode_update(std::span<const Prefix> withdrawn, const PathAttributes* attrs, std::span<const Prefix> nlri,
                    bool as4) {
  if (!nlri.empty() && attrs == nullptr) throw std::invalid_argument("NLRI without path attributes");
  Bytes w;
  for (const auto& p : withdrawn) put_prefix(w, p);
  Bytes a = attrs ? encode_attrs(*attrs, as4) : Bytes{};
  Bytes b;
  put16(b, static_cast<std::uint16_t>(w.size()));
  b.insert(b.end(), w.begin(), w.end());
  put16(b, static_cast<std::uint16_t>(a.size()));
  b.insert(b.end(), a.begin(), a.end());
  for (const auto& p : nlri) put_prefix(b, p);
  if (kHeaderLen + b.size() > kMaxMessageLen) throw std::length_error("UPDATE exceeds 4096 bytes");
  return with_header(MsgType::Update, b);
}

std::vector<Bytes> build_announcements(std::span<const Prefix> nlri, const PathAttributes& attrs, bool as4,
                                       std::size_t max_per_update) {
  std::vector<Bytes> out;
  const std::size_t base = kHeaderLen + 2 + 2 + attrs_size(attrs, as4);
  if (base >= kMaxMessageLen) throw std::length_error("path attributes too large");
  std::size_t i = 0;
  while (i < nlri.size()) {
    std::size_t size = base, j = i;
    while (j < nlri.size() && size + nlri_size(nlri[j]) <= kMaxMessageLen &&
           (max_per_update == 0 || j - i < max_per_update)) {
      size += nlri_size(nlri[j]);
      ++j;
    }
    out.push_back(encode_update({}, &attrs, nlri.subspan(i, j - i), as4));
    i = j;
  }
  return out;
}

std::vector<Bytes> build_withdrawals(std::span<const Prefix> withdrawn, std::size_t max_per_update) {
  std::vector<Bytes> out;
  const std::size_t base = kHeaderLen + 2 + 2;
  std::size_t i = 0;
  while (i < withdrawn.size()) {
    std::size_t size = base, j = i;
    while (j < withdrawn.size() && size + nlri_size(withdrawn[j]) <= kMaxMessageLen &&
           (max_per_update == 0 || j - i < max_per_update)) {
      size += nlri_size(withdrawn[j]);
      ++j;
    }
    out.push_back(encode_update(withdrawn.subspan(i, j - i), nullptr, {}, true));
    i = j;
  }
  return out;
}

// --------------------------------------------------------------- decoder ---
void StreamParser::feed(std::span<const std::uint8_t> data) {
  if (pos_ > 65536 && pos_ * 2 > buf_.size()) {
    buf_.erase(buf_.begin(), buf_.begin() + static_cast<std::ptrdiff_t>(pos_));
    pos_ = 0;
  }
  buf_.insert(buf_.end(), data.begin(), data.end());
}

std::optional<Message> StreamParser::next() {
  if (buffered() < kHeaderLen) return std::nullopt;
  const std::uint8_t* h = buf_.data() + pos_;
  for (int i = 0; i < 16; ++i)
    if (h[i] != 0xFF) throw DecodeError(1, 1, "connection not synchronised (bad marker)");
  const std::size_t len = (static_cast<std::size_t>(h[16]) << 8) | h[17];
  const std::uint8_t type = h[18];
  if (len < kHeaderLen || len > kMaxMessageLen) throw DecodeError(1, 2, "bad message length");
  if (type < 1 || type > 5) throw DecodeError(1, 3, "bad message type");
  if (buffered() < len) return std::nullopt;
  Message m{static_cast<MsgType>(type), Bytes(h + kHeaderLen, h + len)};
  pos_ += len;
  if (pos_ == buf_.size()) { buf_.clear(); pos_ = 0; }
  return m;
}

OpenMsg decode_open(std::span<const std::uint8_t> body) {
  Reader r(body, 2, 0);
  OpenMsg o;
  o.version = r.u8();
  if (o.version != 4) throw DecodeError(2, 1, "unsupported BGP version");
  o.my_as16 = r.u16();
  o.asn = o.my_as16;
  o.hold_time = r.u16();
  if (o.hold_time == 1 || o.hold_time == 2) throw DecodeError(2, 6, "unacceptable hold time");
  o.router_id = r.u32();
  const std::uint8_t opt_len = r.u8();
  Reader params(r.take(opt_len), 2, 0);
  o.caps = Capabilities{false, false, false, 0};
  while (!params.done()) {
    const std::uint8_t ptype = params.u8();
    const std::uint8_t plen = params.u8();
    auto pv = params.take(plen);
    if (ptype != 2) continue;  // only capabilities matter
    o.caps_present = true;
    Reader caps(pv, 2, 0);
    while (!caps.done()) {
      const std::uint8_t code = caps.u8();
      const std::uint8_t len = caps.u8();
      Reader cv(caps.take(len), 2, 0);
      if (code == 1 && len == 4) {
        const std::uint16_t afi = cv.u16();
        cv.u8();
        const std::uint8_t safi = cv.u8();
        if (afi == 1 && safi == 1) o.caps.mp_ipv4_unicast = true;
      } else if (code == 2) {
        o.caps.route_refresh = true;
      } else if (code == 65 && len == 4) {
        o.caps.four_octet_as = true;
        o.caps.asn = cv.u32();
        o.asn = o.caps.asn;
      }
    }
  }
  return o;
}

UpdateMsg decode_update(std::span<const std::uint8_t> body, bool as4) {
  Reader r(body, 3, 1);
  UpdateMsg u;
  const std::uint16_t wlen = r.u16();
  Reader w(r.take(wlen), 3, 1);
  while (!w.done()) u.withdrawn.push_back(read_prefix(w));
  const std::uint16_t alen = r.u16();
  Reader a(r.take(alen), 3, 1);
  std::vector<std::uint32_t> as4_path;
  bool have_as4_path = false;
  while (!a.done()) {
    const std::uint8_t flags = a.u8();
    const std::uint8_t type = a.u8();
    const std::size_t len = (flags & 0x10) ? a.u16() : a.u8();
    auto v = a.take(len);
    Reader vr(v, 3, 5);
    u.has_attrs = true;
    u.attr_types.set(type);
    switch (type) {
      case attr::kOrigin:
        if (len != 1) throw DecodeError(3, 5, "bad ORIGIN length");
        u.attrs.origin = static_cast<Origin>(vr.u8());
        break;
      case attr::kAsPath: u.attrs.as_path = read_as_path(v, as4); break;
      case attr::kNextHop:
        if (len != 4) throw DecodeError(3, 5, "bad NEXT_HOP length");
        u.attrs.next_hop = vr.u32();
        u.attrs.has_next_hop = true;
        break;
      case attr::kMed: u.attrs.med = vr.u32(); break;
      case attr::kLocalPref: u.attrs.local_pref = vr.u32(); break;
      case attr::kCommunities:
        while (!vr.done()) u.attrs.communities.push_back(vr.u32());
        break;
      case attr::kOriginatorId: u.attrs.originator_id = vr.u32(); break;
      case attr::kClusterList:
        while (!vr.done()) u.attrs.cluster_list.push_back(vr.u32());
        break;
      case attr::kAs4Path:
        as4_path = read_as_path(v, true);
        have_as4_path = true;
        break;
      case attr::kLargeCommunity:
        while (!vr.done()) u.attrs.large_communities.push_back({vr.u32(), vr.u32(), vr.u32()});
        break;
      default: break;  // unknown attributes are ignored
    }
  }
  if (!as4 && have_as4_path && as4_path.size() <= u.attrs.as_path.size()) {
    std::vector<std::uint32_t> merged(u.attrs.as_path.begin(),
                                      u.attrs.as_path.end() - static_cast<std::ptrdiff_t>(as4_path.size()));
    merged.insert(merged.end(), as4_path.begin(), as4_path.end());
    u.attrs.as_path = std::move(merged);
  }
  while (!r.done()) u.nlri.push_back(read_prefix(r));
  return u;
}

NotificationMsg decode_notification(std::span<const std::uint8_t> body) {
  Reader r(body, 1, 2);
  NotificationMsg n;
  n.code = r.u8();
  n.subcode = r.u8();
  auto rest = r.take(r.left());
  n.data.assign(rest.begin(), rest.end());
  return n;
}

std::string notification_text(std::uint8_t code, std::uint8_t subcode) {
  static const char* codes[] = {"", "Message Header Error", "OPEN Message Error", "UPDATE Message Error",
                                "Hold Timer Expired", "FSM Error", "Cease"};
  static const char* cease[] = {"", "Maximum number of prefixes reached", "Administrative shutdown",
                                "Peer de-configured", "Administrative reset", "Connection rejected",
                                "Other configuration change", "Connection collision resolution",
                                "Out of resources", "Hard reset", "BFD down"};
  std::string s = code < 7 ? codes[code] : "Unknown error";
  if (code == 6 && subcode < 11) s += std::string(": ") + cease[subcode];
  else if (subcode) s += " (subcode " + std::to_string(subcode) + ")";
  return s;
}

}  // namespace tgen::bgp
