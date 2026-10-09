"""Gallery sharing: a clean customer link by design name (/design/aurora-twist), stable through renames, with a social
preview (ring image, design name, "Designed with XJet Atelier") — and no Ring ID anywhere a customer passes it on."""

import html
import re

from p3.gallery import ShareSlug
from tests.test_gallery import HG, Admin, _Curated  # noqa: F401 — the fixture is used by name


def test_share_slugs_come_from_the_design_name():
    assert ShareSlug("Aurora Twist") == "aurora-twist"
    assert ShareSlug("  Fil   Band ") == "fil-band"
    assert ShareSlug("Éternité Nœud") == "eternite-nud"
    assert ShareSlug("R-1012") == "r-1012"
    assert ShareSlug("") == ""


async def test_share_link_is_by_design_name_stable_and_carries_a_social_preview(HG):
    H = HG
    Did, Cand, Item = await _Curated(H)
    Title = (await H.Design(Did))["title"]
    Slug = ShareSlug(Title)
    Anon = {"X-Access-Token": "NOPE00"}
    R = await H.Client.get(f"/api/gallery/{Item['id']}/share", headers=Anon)         # no sign-in needed
    assert R.status_code == 200, R.text
    S = R.json()
    assert S["slug"] == Slug and S["title"] == Title and S["text"] == "Designed with XJet Atelier"
    assert S["url"] == f"http://p3.test{H.Base}/design/{Slug}"
    assert "R-10" not in S["url"] and "dsg_" not in S["url"] and Item["id"] not in S["url"]   # no technical ids
    assert (await H.Client.get(f"/api/gallery/{Item['id']}/share", headers=Anon)).json()["slug"] == Slug   # stable
    # renaming the design does not break links already sent: the slug stays, the title follows
    R = await H.Client.patch(f"/api/admin/designs/{Did}", json={"title": "Aurora Lattice"}, headers=Admin)
    assert R.status_code == 200, R.text
    S2 = (await H.Client.get(f"/api/gallery/{Item['id']}/share", headers=Anon)).json()
    assert S2["slug"] == Slug and S2["url"] == S["url"] and S2["title"] == "Aurora Lattice"
    # the share page: the site, with the design's preview to open and the tags a messaging app reads
    R = await H.Client.get(f"/design/{Slug}", headers=Anon)
    assert R.status_code == 200 and R.headers["content-type"].startswith("text/html")
    assert R.headers["cache-control"] == "no-cache"
    Html = R.text
    assert "<title>Aurora Lattice · XJet Atelier</title>" in Html
    assert '<meta property="og:title" content="Aurora Lattice">' in Html
    assert '<meta property="og:site_name" content="XJet Atelier">' in Html
    assert re.search(r'<meta property="og:description" content="Designed with XJet Atelier[^"]*">', Html)
    Img = html.unescape(re.search(r'<meta property="og:image" content="([^"]+)">', Html).group(1))   # attributes escape "&"
    assert Img.startswith(f"http://p3.test{H.Base}/thumb/designs/") and Img.endswith("?w=800&f=jpg")
    assert (await H.Client.get(Img.removeprefix("http://p3.test" + H.Base), headers=Anon)).headers["content-type"] == "image/jpeg"
    assert f'<meta property="og:url" content="{S["url"]}">' in Html and '<meta name="twitter:card" content="summary_large_image">' in Html
    Open = re.search(r"window\.__p3Open = (\{.*?\});", Html).group(1)
    assert f'"gallery": "{Item["id"]}"' in Open and '"site_title": "XJet Atelier' in Open
    Words = "\n".join(re.findall(r'<title>[^<]*|content="[^"]*"', Html.split("<body")[0]))
    assert "R-1" not in Words                                                        # no Ring ID in what a preview shows
    assert 'src="' + H.Base + '/static/app.js?v=' in Html                           # the normal, versioned app
    # the link by the current name also resolves; an unknown name gives the site with a 404 and no design to open
    R = await H.Client.get("/design/aurora-lattice", headers=Anon)
    assert R.status_code == 200 and f'"gallery": "{Item["id"]}"' in R.text
    R = await H.Client.get("/design/no-such-ring", headers=Anon)
    assert R.status_code == 404 and 'window.__p3Open = {"gallery": null};' in R.text and "<title>XJet Atelier" in R.text
    assert (await H.Client.get("/api/gallery/gi_nope/share", headers=Anon)).status_code == 404


async def test_two_designs_with_the_same_name_get_distinct_links(HG):
    H = HG
    _, _, I1 = await _Curated(H)
    B2 = await H.NewDesign("Twisted bands joined by a small knot")
    await H.Client.put(f"/api/designs/{B2['design_id']}/selection", json={"candidate_id": B2["candidates"][0]["id"]})
    Same = (await H.Design(I1["design_id"]))["title"]
    await H.Client.patch(f"/api/admin/designs/{B2['design_id']}", json={"title": Same, "force": True}, headers=Admin)
    I2 = (await H.Client.post("/api/admin/gallery", json={"design_id": B2["design_id"]}, headers=Admin)).json()
    S1 = (await H.Client.get(f"/api/gallery/{I1['id']}/share")).json()
    S2 = (await H.Client.get(f"/api/gallery/{I2['id']}/share")).json()
    assert S1["slug"] != S2["slug"] and S2["slug"] == S1["slug"] + "-2"
    assert f'"gallery": "{I2["id"]}"' in (await H.Client.get(f"/design/{S2['slug']}")).text
