# ABOUTME: Primary US radio and communications law references for the /law quick-reference page.
# ABOUTME: Teaches law as knowledge (D16), citing each statute, rule, or case behind M5's cards.

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LawSection:
    """One authority behind the course's legal rules, shown on /law."""

    id: str
    title: str
    cite: str
    summary: str
    url: str


# Mirrors M5's explain caution and states clearly that the page teaches principles, not counsel.
LAW_CAUTION = (
    "Specifics vary by country and, for listening, by state — treat this as the shape of the law, "
    "not legal advice; check your state and local rules before you rely on it."
)

LAW_SECTIONS: tuple[LawSection, ...] = (
    LawSection(
        id="wiretap-open",
        title="Listening to open broadcasts, aircraft, ships, and public safety",
        cite="18 USC 2511(2)(g)(i)-(ii)",
        summary=(
            "Under the federal Wiretap Act, it is not unlawful to receive electronic "
            "communications readily accessible to the general public, or radio transmissions "
            "intended for the public, from marine or aeronautical communications systems, or "
            "from unencrypted government, police, fire, or public-safety systems. Some states "
            "restrict mobile scanners in vehicles."
        ),
        url="https://www.law.cornell.edu/uscode/text/18/2511",
    ),
    LawSection(
        id="wiretap-limits",
        title="Encryption, cellular calls, and commercial pagers",
        cite="18 USC 2510(12)(B), 2510(16)(A)/(D), 2511(1)(a)",
        summary=(
            "Scrambled or encrypted transmissions and common-carrier traffic (phone calls and "
            "commercial text/numeric pagers; only tone-only pagers are carved out) do not count "
            "as readily accessible to the general public. Intentionally intercepting or "
            "decrypting them is a federal offence on its own — which is why this console shows "
            "POCSAG framing and withholds the words."
        ),
        url="https://www.law.cornell.edu/uscode/text/18/2510",
    ),
    LawSection(
        id="joffe-wifi",
        title="Wi-Fi metadata versus payload (Joffe v. Google)",
        cite="Joffe v. Google, Inc., 746 F.3d 920 (9th Cir. 2013)",
        summary=(
            "Access points broadcast beacons (channel, SSID, BSSID, signal, security) so devices "
            "can see them, and a read-only survey counts those envelopes without connecting. "
            "Payload data — emails, passwords, web traffic — is protected even on open Wi-Fi: "
            "the Ninth Circuit held it is neither a 'radio communication' under 18 USC 2510(16) "
            "nor 'readily accessible to the general public' under 18 USC 2511(2)(g)(i)."
        ),
        url="https://www.govinfo.gov/content/pkg/USCOURTS-ca9-11-17483/pdf/USCOURTS-ca9-11-17483-1.pdf",
    ),
    LawSection(
        id="divulge-605",
        title="Using or publishing private radio interceptions",
        cite="47 USC 605(a)",
        summary=(
            "The Communications Act bars divulging, publishing, or using the existence or "
            "contents of private radio communications you are not authorised to receive, while "
            "carving out broadcasts for the general public, amateur traffic, and distress calls."
        ),
        url="https://www.law.cornell.edu/uscode/text/47/605",
    ),
    LawSection(
        id="tx-licence",
        title="Transmitting requires a licence or rule authority",
        cite="47 USC 301; 47 CFR 97.5(a)",
        summary=(
            "Operating radio transmitting apparatus in the US requires an FCC licence unless a "
            "specific rule authorizes licence-free operation. On the amateur bands, the station "
            "must be under the control of a licensed amateur operator before keying up."
        ),
        url="https://www.law.cornell.edu/uscode/text/47/301",
    ),
    LawSection(
        id="part15-ism",
        title="Licence-free 915 MHz ISM (Part 15) and waveform rules",
        cite="47 CFR 15.247(a)(2), 15.249",
        summary=(
            "Compliant Part 15 equipment may transmit without an individual licence inside its "
            "power, antenna, region, and no-interference limits. On 902-928 MHz, single-channel "
            "250 kHz presets (LongFast) fall under 15.249 (50 mV/m at 3 m, ~0.75 mW EIRP), while "
            "500 kHz digital modulation (Long Turbo) meets 15.247(a)(2) and its 1 W limit."
        ),
        url="https://www.ecfr.gov/current/title-47/section-15.247",
    ),
    LawSection(
        id="part95-prs",
        title="Shared UHF walkie-talkies: FRS versus GMRS",
        cite="47 CFR 95.305, 95.1705",
        summary=(
            "Personal Radio Services let compliant FRS handhelds transmit without an individual "
            "licence (95.305), whereas operating a GMRS station on the same 462 MHz channels "
            "(higher power, detachable antennas, repeaters) requires a valid individual GMRS "
            "licence (95.1705)."
        ),
        url="https://www.law.cornell.edu/cfr/text/47/95.1705",
    ),
    LawSection(
        id="scanner-15121",
        title="Owning a wideband SDR receiver",
        cite="47 CFR 15.121",
        summary=(
            "The cellular-band scanner rule governs which scanning receivers may be certified "
            "and marketed in the US; it does not outlaw owning a general-purpose receiver or SDR "
            "for lawful passive listening."
        ),
        url="https://www.law.cornell.edu/cfr/text/47/15.121",
    ),
)
