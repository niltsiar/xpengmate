defmodule TeslaMateWeb.XpengMateController do
  @moduledoc """
  Plain multipart upload for XPENG data-export zips, and the form that asks the
  dataact service to request a new export from XPENG.

  The LiveView file input proved unreliable for large, multi-file selections
  (the first entry of a multi-select never reached the server), so the export
  card posts a normal multipart form here. Plug streams the parts to temp files,
  the controller forwards them to the history service, and the redirect carries
  the result back to the settings page.
  """

  use TeslaMateWeb, :controller

  require Logger

  alias TeslaMate.XpengMate.Client

  @max_files 12

  def upload_exports(conn, params) do
    files =
      params
      |> Map.get("files", [])
      |> List.wrap()
      |> Enum.reject(&(&1 in [nil, ""]))
      |> Enum.flat_map(fn
        %Plug.Upload{filename: filename, path: path} -> [{filename, File.read!(path)}]
        _ -> []
      end)

    cond do
      files == [] ->
        redirect_with(conn, :error, gettext("Choose at least one export zip first."))

      length(files) > @max_files ->
        redirect_with(
          conn,
          :error,
          gettext("Too many files: upload at most %{max} at a time.", max: @max_files)
        )

      true ->
        case Client.upload_exports(files) do
          {:ok, _} ->
            redirect_with(
              conn,
              :info,
              gettext(
                "Uploaded %{count} file(s). The import is running in the background; this page refreshes itself.",
                count: length(files)
              )
            )

          {:error, reason} ->
            Logger.info("XpengMate export upload rejected: #{inspect(reason)}")

            redirect_with(conn, :error, reason)
        end
    end
  end

  def request_export(conn, params) do
    email = params |> text_param("email") |> String.trim()
    password = text_param(params, "password")

    cond do
      Client.dataact_url() == nil ->
        redirect_with(conn, :error, gettext("Requesting the export is not configured."))

      not over_https?(conn) ->
        redirect_with(
          conn,
          :error,
          gettext(
            "Open this page over HTTPS to request the export: the password must not cross the network in clear."
          )
        )

      email == "" or password == "" ->
        redirect_with(
          conn,
          :error,
          gettext("Enter the email and the password of the XPENG account.")
        )

      true ->
        case Client.request_export(email, password) do
          {:ok, _} ->
            redirect_with(
              conn,
              :info,
              gettext(
                "Export requested. The job takes a few minutes; this page shows its progress."
              )
            )

          {:error, reason} ->
            Logger.info("XpengMate export request refused: #{reason}")
            redirect_with(conn, :error, request_error(reason))
        end
    end
  end

  # The app sits behind a TLS proxy; a direct request to the app port is plain HTTP.
  defp over_https?(conn) do
    conn.scheme == :https or get_req_header(conn, "x-forwarded-proto") == ["https"]
  end

  defp text_param(params, key) do
    case Map.get(params, key) do
      value when is_binary(value) -> value
      _ -> ""
    end
  end

  defp request_error(:running), do: gettext("An export request is already running.")

  defp request_error(:cooldown),
    do: gettext("A recent XPENG login failed. Wait 15 minutes, then try again.")

  defp request_error(:unreachable), do: gettext("The export request service is unreachable.")
  defp request_error(_), do: gettext("The export request service refused the request.")

  defp redirect_with(conn, kind, message) do
    conn
    |> put_flash(kind, message)
    |> redirect(to: "/xpengmate")
  end
end
