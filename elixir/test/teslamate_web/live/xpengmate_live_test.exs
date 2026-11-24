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

  describe "export request" do
    test "hides the request form when dataact is not configured", %{conn: conn} do
      with_mocks [client_mock({:error, :not_configured})] do
        assert {:ok, _view, html} = live(conn, "/xpengmate")

        assert find(html, "#exports-form") != []
        assert find(html, "#export-request-form") == []
      end
    end

    test "shows the form and a status error when dataact is unreachable", %{conn: conn} do
      with_mocks [client_mock({:error, "service unreachable"})] do
        assert {:ok, _view, html} = live(conn, "/xpengmate")

        assert [_form] = find(html, "#export-request-form")
        assert html =~ "Export request status unavailable: service unreachable"
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
