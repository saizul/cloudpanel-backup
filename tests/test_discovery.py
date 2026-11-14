from pathlib import Path

from cloudpanel_backup.discovery import (
    discover_sites,
    looks_like_domain,
    master_credentials,
    parse_master_credentials,
    split_site_path,
)
from conftest import FakeRunner, make_config


def test_discovers_vhost_and_filesystem_sites(server):
    sites = discover_sites(make_config(server))
    by_domain = {s.domain: s for s in sites}
    assert set(by_domain) == {"shop.example.com", "www.blog.example.org"}
    shop = by_domain["shop.example.com"]
    assert shop.source == "nginx"
    assert shop.kind == "wordpress"
    assert shop.user == "shop"
    assert shop.database_names == ["shop_wp"]
    assert shop.server_names == ["shop.example.com", "www.shop.example.com"]
    blog = by_domain["www.blog.example.org"]
    assert blog.source == "filesystem"
    assert blog.databases == []


def test_clp_user_is_never_a_site(server):
    assert all(s.user != "clp" for s in discover_sites(make_config(server)))


def test_site_filters_and_overrides(server):
    cfg = make_config(server, backup={"exclude_sites": ["www.*"]})
    assert [s.domain for s in discover_sites(cfg)] == ["shop.example.com"]
    cfg = make_config(server)
    from cloudpanel_backup.config import SiteOverride

    cfg.sites["shop.example.com"] = SiteOverride(
        "shop.example.com", databases=["shop_wp", "shop_extra"], exclude=["private"]
    )
    cfg.sites["www.blog.example.org"] = SiteOverride("www.blog.example.org", skip=True)
    sites = discover_sites(cfg)
    assert [s.domain for s in sites] == ["shop.example.com"]
    assert sites[0].database_names == ["shop_wp", "shop_extra"]
    assert sites[0].databases[0].user == "shop_user"  # credentials kept from wp-config.php
    assert sites[0].excludes == ["private"]


def test_databases_disabled(server):
    cfg = make_config(server, backup={"databases": False})
    assert all(not s.databases for s in discover_sites(cfg))


def test_split_site_path():
    home = Path("/home")
    assert split_site_path("/home/john/htdocs/www.example.com/public", home) == (
        "john",
        Path("/home/john/htdocs/www.example.com"),
    )
    assert split_site_path("/var/www/html", home) is None
    assert split_site_path("/home/john/www", home) is None


def test_looks_like_domain():
    assert looks_like_domain("www.example.com")
    assert not looks_like_domain("default")
    assert not looks_like_domain("-bad.example")


def test_parse_master_credentials_table_and_lines():
    creds = parse_master_credentials(FakeRunner().master_output)
    assert creds is not None
    assert (creds.user, creds.password, creds.port) == ("root", "master-secret", 3306)
    creds = parse_master_credentials("Host: 127.0.0.1\nUser Name: root\nPassword: abc:def\n")
    assert creds is not None and creds.password == "abc:def"
    assert parse_master_credentials("nothing useful") is None


def test_master_credentials_needs_clpctl():
    assert master_credentials(FakeRunner(programs=["mariadb-dump"])) is None
    runner = FakeRunner()
    runner.fail["clpctl"] = "boom"
    assert master_credentials(runner) is None
    assert master_credentials(FakeRunner()).user == "root"
