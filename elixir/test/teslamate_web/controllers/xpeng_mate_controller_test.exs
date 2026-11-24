defmodule TeslaMateWeb.XpengMateControllerTest do
  use TeslaMateWeb.ConnCase

  import Mock

  alias TeslaMate.XpengMate.Client

  @path "/xpengmate/export-request"
  @fields %{"email" => "owner@example.com", "password" => "secret"}

  defp client_mock(url) do
    {Client, [],
     [dataact_url: fn -> url end, request_export: fn _email, _password -> {:ok, %{}} end]}
  end

  defp over_https(conn), do: put_req_header(conn, "x-forwarded-proto", "https")

  describe "request_export" do
    test "sends the email and the password to dataact once over HTTPS", %{conn: conn} do
      with_mocks [client_mock("http://dataact:8300")] do
        conn = conn |> over_https() |> post(@path, @fields)

        assert redirected_to(conn) == "/xpengmate"
        assert Phoenix.Flash.get(conn.assigns.flash, :info) =~ "Export requested"
        assert_called_exactly(Client.request_export("owner@example.com", "secret"), 1)
      end
    end

    test "refuses a request without a password and sends nothing", %{conn: conn} do
      with_mocks [client_mock("http://dataact:8300")] do
        conn = conn |> over_https() |> post(@path, %{"email" => "owner@example.com"})

        assert redirected_to(conn) == "/xpengmate"
        assert Phoenix.Flash.get(conn.assigns.flash, :error) =~ "Enter the email and the password"
        assert_not_called(Client.request_export(:_, :_))
      end
    end

    test "refuses a request over plain HTTP and sends nothing", %{conn: conn} do
      with_mocks [client_mock("http://dataact:8300")] do
        conn = post(conn, @path, @fields)

        assert redirected_to(conn) == "/xpengmate"
        assert Phoenix.Flash.get(conn.assigns.flash, :error) =~ "HTTPS"
        assert_not_called(Client.request_export(:_, :_))
      end
    end

    test "refuses a request when dataact is not configured and sends nothing", %{conn: conn} do
      with_mocks [client_mock(nil)] do
        conn = conn |> over_https() |> post(@path, @fields)

        assert redirected_to(conn) == "/xpengmate"
        assert Phoenix.Flash.get(conn.assigns.flash, :error) =~ "not configured"
        assert_not_called(Client.request_export(:_, :_))
      end
    end
  end

  test "the request log filters the email and the password" do
    assert Phoenix.Logger.filter_values(%{"email" => "x", "password" => "y"}) ==
             %{"email" => "[FILTERED]", "password" => "[FILTERED]"}
  end
end
