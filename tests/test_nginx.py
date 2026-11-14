from pathlib import Path

from cloudpanel_backup.nginx import load_vhosts, parse_vhost_text, tokenize

CLOUDPANEL_VHOST = r"""
server {
  listen 80;
  listen [::]:80;
  listen 443 quic;
  listen 443 ssl;
  http2 on;
  ssl_certificate_key /etc/nginx/ssl-certificates/www.example.com.key;
  server_name www.example.com example.com;
  root /home/example/htdocs/www.example.com/public;

  if ($scheme != "https") {
    rewrite ^ https://$host$request_uri permanent;
  }

  location ~ \.php$ {
    include fastcgi_params;
    fastcgi_pass 127.0.0.1:17003;
  }
}

server {
  listen 8080;
  server_name www.example.com;
  root /home/example/htdocs/www.example.com/public;
}
"""


def test_tokenize_handles_quotes_and_comments():
    assert tokenize('a "b c" # x;y\n d;') == ["a", "b c", "d", ";"]
    assert tokenize("set $x ${var}a;") == ["set", "$x", "${var}a", ";"]


def test_parse_cloudpanel_php_vhost():
    vhost = parse_vhost_text(CLOUDPANEL_VHOST)
    assert len(vhost.servers) == 2
    assert vhost.server_names == ["www.example.com", "example.com"]
    assert vhost.root == "/home/example/htdocs/www.example.com/public"
    assert vhost.uses_php
    assert vhost.proxy_pass is None
    assert "443 ssl" in vhost.servers[0].listens


def test_parse_reverse_proxy_vhost():
    vhost = parse_vhost_text(
        """
        server { server_name app.example.com; root /home/app/htdocs/app.example.com;
          location / { proxy_pass http://127.0.0.1:3000; } }
        """
    )
    assert vhost.proxy_pass == "http://127.0.0.1:3000"
    assert not vhost.uses_php


def test_default_server_names_are_ignored():
    vhost = parse_vhost_text("server { server_name _ localhost; }")
    assert vhost.server_names == []


def test_load_vhosts_skips_missing_dir(tmp_path: Path):
    assert load_vhosts(tmp_path / "missing") == []
    (tmp_path / "a.conf").write_text("server { server_name a.example; }")
    (tmp_path / "sub").mkdir()
    assert [v.server_names for v in load_vhosts(tmp_path)] == [["a.example"]]
