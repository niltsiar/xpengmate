defmodule TeslaMateWeb.XpengMateLiveTest do
  use TeslaMateWeb.ConnCase

  import Mock

  alias TeslaMate.XpengMate.Client

  defp client_mock(dataact_status) do
    {Client, [],
     [
       engine_status: fn -> {:error, "service unreachable"} end,
       history_status: fn -> {:error, "service unreachable"} end,
       exports_status: fn -> {:ok, %{"running" => false, "imports" => []}} end,
       dataact_status: fn -> dataact_status end
     ]}
  end

  defp find(html, selector), do: html |> Floki.parse_document!() |> Floki.find(selector)

  defp over_https(conn), do: put_req_header(conn, "x-forwarded-proto", "https")

  defp attrs(html, selector) do
    html |> find(selector) |> Enum.map(fn {_tag, attrs, _children} -> Map.new(attrs) end)
  end

  describe "export request" do
    test "hides the request form when dataact is not configured", %{conn: conn} do
      with_mocks [client_mock({:error, :not_configured})] do
        assert {:ok, _view, html} = live(over_https(conn), "/xpengmate")

        assert find(html, "#exports-form") != []
        assert find(html, "#export-request-form") == []
      end
    end

    test "shows the form and a status error when dataact is unreachable", %{conn: conn} do
      with_mocks [client_mock({:error, "service unreachable"})] do
        assert {:ok, _view, html} = live(over_https(conn), "/xpengmate")

        assert [_form] = find(html, "#export-request-form")
        assert html =~ "Export request status unavailable: service unreachable"
      end
    end

    test "a page loaded over HTTPS renders the form with a CSRF token in the first response",
         %{conn: conn} do
      with_mocks [client_mock({:ok, %{"state" => "idle"}})] do
        dead_html = conn |> over_https() |> get("/xpengmate") |> html_response(200)
        assert {:ok, _view, live_html} = live(over_https(conn), "/xpengmate")

        for html <- [dead_html, live_html] do
          assert [%{"action" => "/xpengmate/export-request", "method" => "post"}] =
                   attrs(html, "#export-request-form")

          assert [%{"type" => "hidden", "value" => token}] =
                   attrs(html, "#export-request-form input[name='_csrf_token']")

          assert token != ""

          assert [%{"type" => "email", "autocomplete" => "username"}] =
                   attrs(html, "#export-request-form input[name='email']")

          assert [%{"type" => "password", "autocomplete" => "current-password"}] =
                   attrs(html, "#export-request-form input[name='password']")
        end
      end
    end

    test "a page loaded over plain HTTP hides the form and asks for HTTPS", %{conn: conn} do
      with_mocks [client_mock({:ok, %{"state" => "idle"}})] do
        dead_html = conn |> get("/xpengmate") |> html_response(200)
        assert {:ok, _view, live_html} = live(conn, "/xpengmate")

        for html <- [dead_html, live_html] do
          assert find(html, "#export-request-form") == []
          assert html |> find("#export-request-https") |> Floki.text() =~ "over HTTPS"
        end
      end
    end

    test "shows the step of a running job and keeps one reload timer", %{conn: conn} do
      status = %{
        "state" => "running",
        "step" => "login",
        "message" => "logging in",
        "finished_at" => nil
      }

      with_mocks [client_mock({:ok, status})] do
        assert {:ok, view, html} = live(conn, "/xpengmate")

        assert [line] = find(html, "#export-request-status")
        assert Floki.text(line) =~ "running, step login, logging in"

        timer = refresh_timer(view)
        assert is_integer(Process.read_timer(timer))

        render_click(view, "refresh")
        assert refresh_timer(view) == timer
      end
    end
  end

  defp refresh_timer(view), do: :sys.get_state(view.pid).socket.assigns.refresh_timer
end
