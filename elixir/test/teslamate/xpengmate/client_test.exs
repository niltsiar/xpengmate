defmodule TeslaMate.XpengMate.ClientTest do
  use ExUnit.Case

  import Mock

  alias TeslaMate.XpengMate.Client

  setup do
    System.put_env("XPENGMATE_DATAACT_URL", "http://dataact:8300")
    System.put_env("XPENGMATE_DATAACT_TOKEN", "dataact-token")

    on_exit(fn ->
      System.delete_env("XPENGMATE_DATAACT_URL")
      System.delete_env("XPENGMATE_DATAACT_TOKEN")
    end)
  end

  defp dataact_mock(status, body) do
    test = self()

    {Tesla.Adapter.Finch, [],
     call: fn env, _opts ->
       send(test, {:request, env})

       {:ok,
        %Tesla.Env{
          status: status,
          headers: [{"content-type", "application/json"}],
          body: Jason.encode!(body)
        }}
     end}
  end

  describe "request_export" do
    test "posts the account as JSON with the dataact token" do
      with_mocks [dataact_mock(202, %{"state" => "running"})] do
        assert {:ok, %{"state" => "running"}} =
                 Client.request_export("owner@example.com", "secret")

        assert_received {:request, env}
        assert env.method == :post
        assert env.url == "http://dataact:8300/request"
        assert Tesla.get_header(env, "authorization") == "Bearer dataact-token"

        assert Jason.decode!(env.body) == %{
                 "email" => "owner@example.com",
                 "password" => "secret"
               }
      end
    end

    test "maps a 409 by its reason field, not by the error text" do
      cases = [
        {"cooldown", "an export request is already running", :cooldown},
        {"running", "a login failed; try again later", :running},
        {"other", "an export request is already running", :rejected}
      ]

      for {reason, error, expected} <- cases do
        with_mocks [dataact_mock(409, %{"error" => error, "reason" => reason})] do
          assert Client.request_export("owner@example.com", "secret") == {:error, expected}
        end
      end
    end
  end
end
