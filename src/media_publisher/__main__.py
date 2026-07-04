from __future__ import annotations

import argparse
import sys
from pathlib import Path

from media_publisher.config import load_settings
from media_publisher.publishers.facebook import FacebookPublishError, publish_to_facebook
from media_publisher.publishers.instagram import InstagramPublishError, publish_to_instagram
from media_publisher.publishers.meta import (
    MetaClient,
    MetaError,
    MetaPageInfo,
    normalize_facebook_page_username,
    normalize_instagram_username,
)
from media_publisher.publishers.youtube import YouTubeClient, YouTubePublishError
from media_publisher.sources.airtable import (
    AirtableClient,
    AirtableError,
    FIELD_FACEBOOK_POST_ID,
    FIELD_INSTAGRAM_MEDIA_ID,
    record_to_publish_job,
)
from media_publisher.sources.canva import (
    CanvaClient,
    CanvaError,
    download_images_from_canva_url,
    parse_design_id,
    resolve_canva_url,
)
from media_publisher.sources.happyscribe import (
    HappyScribeClient,
    HappyScribeError,
    TRANSCRIPTION_STATE_READY,
    burned_video_destination_path,
    is_subtitled_export_name,
    resolve_library_location,
)
from media_publisher.sources.happyscribe_web import (
    HappyScribeWebError,
    export_video_with_subtitles_web,
    import_browser_session,
    save_browser_session_interactive,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def configure_console_output() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def print_console(text: str) -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Extract publishing metadata from Airtable, HappyScribe, and Canva, "
            "then publish to YouTube, Facebook, and Instagram."
        )
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate required environment variables and exit.",
    )
    parser.add_argument(
        "--test-airtable",
        action="store_true",
        help="Verify Airtable credentials by reading up to one record.",
    )
    parser.add_argument(
        "--test-happyscribe",
        action="store_true",
        help="Verify HappyScribe credentials by listing organizations and transcriptions.",
    )
    parser.add_argument(
        "--list-happyscribe-library",
        action="store_true",
        help="List videos in the configured HappyScribe library folder.",
    )
    parser.add_argument(
        "--download-happyscribe-library",
        action="store_true",
        help=(
            "Download ready source videos from the configured HappyScribe library folder "
            "with subtitles exported through the HappyScribe web session."
        ),
    )
    parser.add_argument(
        "--export-happyscribe-web",
        metavar="TRANSCRIPTION_ID",
        help="Export one HappyScribe video with styled burned-in subtitles via the web session.",
    )
    parser.add_argument(
        "--happyscribe-export-headless",
        action="store_true",
        help="Run the HappyScribe web export browser in headless mode.",
    )
    parser.add_argument(
        "--happyscribe-save-session",
        action="store_true",
        help="Open HappyScribe in Chrome/Edge and save an authenticated session for web export.",
    )
    parser.add_argument(
        "--happyscribe-import-session",
        metavar="PATH",
        help="Import a Playwright storage-state JSON file exported from a normal browser login.",
    )
    parser.add_argument(
        "--burn-happyscribe-video",
        metavar="TRANSCRIPTION_ID",
        help="Fallback: burn exported SRT subtitles locally with ffmpeg (no HappyScribe styling).",
    )
    parser.add_argument(
        "--canva-auth",
        action="store_true",
        help="Print the Canva OAuth authorization URL and save pending PKCE state.",
    )
    parser.add_argument(
        "--canva-auth-code",
        metavar="CODE",
        help="Exchange a Canva authorization code for a token (after --canva-auth).",
    )
    parser.add_argument(
        "--canva-auth-state",
        metavar="STATE",
        help="Optional state value returned by Canva during OAuth.",
    )
    parser.add_argument(
        "--test-canva",
        action="store_true",
        help="Verify Canva credentials by refreshing the stored OAuth token.",
    )
    parser.add_argument(
        "--canva-download",
        metavar="URL",
        help="Download image(s) from a Canva design URL or canva.link short link.",
    )
    parser.add_argument(
        "--canva-format",
        choices=("png", "jpg"),
        default="png",
        help="Image format for --canva-download (default: png).",
    )
    parser.add_argument(
        "--canva-resolve",
        metavar="URL",
        help="Resolve a canva.link short URL and print the design ID (no API auth needed).",
    )
    parser.add_argument(
        "--youtube-auth",
        action="store_true",
        help="Print the YouTube OAuth authorization URL and save pending state.",
    )
    parser.add_argument(
        "--youtube-auth-code",
        metavar="CODE",
        help="Exchange a YouTube authorization code for a token (after --youtube-auth).",
    )
    parser.add_argument(
        "--youtube-auth-state",
        metavar="STATE",
        help="Optional state value returned by Google during OAuth.",
    )
    parser.add_argument(
        "--test-youtube",
        action="store_true",
        help="Verify YouTube credentials by refreshing the stored OAuth token.",
    )
    parser.add_argument(
        "--test-meta",
        action="store_true",
        help="Verify Meta credentials for the configured Sadhguru Bulgarian accounts.",
    )
    parser.add_argument(
        "--resolve-meta",
        action="store_true",
        help="Resolve Facebook Page and Instagram account IDs from configured usernames.",
    )
    parser.add_argument(
        "--schedule-facebook",
        metavar="RECORD_ID",
        help="Schedule or publish a video to Facebook from an Airtable record.",
    )
    parser.add_argument(
        "--schedule-instagram",
        metavar="RECORD_ID",
        help="Schedule or publish a Reel to Instagram from an Airtable record.",
    )
    return parser


def airtable_client_from_settings(settings) -> AirtableClient:
    return AirtableClient(
        token=settings.airtable_token,
        base_id=settings.airtable_base_id,
        table_name=settings.airtable_table_name,
        api_base=settings.airtable_api_base,
        view=settings.airtable_view,
    )


def happyscribe_client_from_settings(settings) -> HappyScribeClient:
    return HappyScribeClient(
        api_key=settings.happyscribe_api_key or "",
        api_base=settings.happyscribe_api_base,
        organization_id=settings.happyscribe_organization_id,
        ffmpeg_path=settings.happyscribe_ffmpeg,
    )


def happyscribe_library_from_settings(settings):
    return resolve_library_location(
        library_url=settings.happyscribe_library_url,
        organization_id=settings.happyscribe_organization_id,
        folder_id=settings.happyscribe_folder_id,
    )


def happyscribe_settings_missing(settings) -> list[str]:
    missing = []
    if not settings.happyscribe_api_key:
        missing.append("HAPPYSCRIBE_API_KEY")
    return missing


def happyscribe_browser_state_path(settings) -> Path:
    return PROJECT_ROOT / settings.happyscribe_browser_state


def happyscribe_browser_profile_path(settings) -> Path:
    return PROJECT_ROOT / settings.happyscribe_browser_profile


def happyscribe_web_export_kwargs(settings, args) -> dict:
    return {
        "browser_state_path": happyscribe_browser_state_path(settings),
        "browser_profile_dir": happyscribe_browser_profile_path(settings),
        "browser_channel": settings.happyscribe_browser_channel,
        "api_key": settings.happyscribe_api_key,
        "headless": args.happyscribe_export_headless,
    }


def happyscribe_web_settings_missing(settings) -> list[str]:
    missing = happyscribe_settings_missing(settings)
    if not happyscribe_browser_state_path(settings).exists():
        missing.append(f"browser session ({settings.happyscribe_browser_state})")
    return missing


def happyscribe_library_settings_missing(settings) -> list[str]:
    missing = happyscribe_web_settings_missing(settings)
    if not (
        settings.happyscribe_library_url
        or (settings.happyscribe_organization_id and settings.happyscribe_folder_id)
    ):
        missing.append(
            "HAPPYSCRIBE_LIBRARY_URL or (HAPPYSCRIBE_ORGANIZATION_ID + HAPPYSCRIBE_FOLDER_ID)"
        )
    return missing


def canva_settings_missing(settings) -> list[str]:
    missing = []
    if not settings.canva_client_id:
        missing.append("CANVA_CLIENT_ID")
    if not settings.canva_client_secret:
        missing.append("CANVA_CLIENT_SECRET")
    if not (PROJECT_ROOT / settings.canva_token).exists():
        missing.append(f"token file ({settings.canva_token})")
    return missing


def canva_client_from_settings(settings) -> CanvaClient:
    return CanvaClient(
        client_id=settings.canva_client_id or "",
        client_secret=settings.canva_client_secret or "",
        token_path=PROJECT_ROOT / settings.canva_token,
        api_base=settings.canva_api_base,
        redirect_uri=settings.canva_redirect_uri,
    )


def canva_settings_complete(settings) -> bool:
    return bool(
        settings.canva_client_id
        and settings.canva_client_secret
        and (PROJECT_ROOT / settings.canva_token).exists()
    )


def meta_settings_complete(settings) -> bool:
    return bool(settings.meta_access_token)


def meta_client_from_settings(settings) -> MetaClient:
    return MetaClient(
        settings.meta_access_token or "",
        api_version=settings.meta_api_version,
        app_id=settings.meta_app_id,
    )


def meta_settings_missing(settings) -> list[str]:
    missing = []
    if not settings.meta_access_token:
        missing.append("META_ACCESS_TOKEN")
    return missing


def meta_facebook_url(settings) -> str:
    username = normalize_facebook_page_username(settings.meta_page_username)
    return f"https://www.facebook.com/{username}"


def meta_instagram_url(settings) -> str:
    username = normalize_instagram_username(settings.meta_instagram_username)
    return f"https://www.instagram.com/{username}/"


def resolve_meta_targets(settings) -> tuple[str, str, MetaPageInfo]:
    client = meta_client_from_settings(settings)
    page_info = client.resolve_page_by_username(settings.meta_page_username)
    client.verify_instagram_username(page_info, settings.meta_instagram_username)

    if settings.meta_page_id and settings.meta_page_id != page_info.page_id:
        raise MetaError(
            f"META_PAGE_ID {settings.meta_page_id!r} does not match "
            f"Facebook page {settings.meta_page_username!r}"
        )

    page_id = settings.meta_page_id or page_info.page_id
    instagram_account_id = settings.meta_instagram_account_id or page_info.instagram_account_id
    if not instagram_account_id:
        raise MetaError(
            "No Instagram business account is linked to the Facebook page. "
            f"Connect {meta_instagram_url(settings)} in Meta Business Suite."
        )

    if (
        settings.meta_instagram_account_id
        and page_info.instagram_account_id
        and settings.meta_instagram_account_id != page_info.instagram_account_id
    ):
        raise MetaError(
            f"META_INSTAGRAM_ACCOUNT_ID {settings.meta_instagram_account_id!r} does not match "
            f"the account linked to {settings.meta_page_username!r}"
        )

    return page_id, instagram_account_id, page_info


def load_publish_job_from_airtable(settings, record_id: str):
    client = airtable_client_from_settings(settings)
    record = client.get_record(record_id)
    return record_to_publish_job(record), client


def youtube_client_from_settings(settings) -> YouTubeClient:
    return YouTubeClient(
        client_secrets_path=PROJECT_ROOT / settings.youtube_client_secrets,
        token_path=PROJECT_ROOT / settings.youtube_token,
        expected_channel_handle=settings.youtube_channel_handle,
    )


def youtube_settings_complete(settings) -> bool:
    return bool(
        (PROJECT_ROOT / settings.youtube_client_secrets).exists()
        and (PROJECT_ROOT / settings.youtube_token).exists()
    )


def main() -> int:
    configure_console_output()
    settings = load_settings(PROJECT_ROOT)
    parser = build_parser()
    args = parser.parse_args()

    if args.check_config:
        missing = []
        if not settings.airtable_token:
            missing.append("AIRTABLE_TOKEN")
        if not settings.airtable_base_id:
            missing.append("AIRTABLE_BASE_ID")
        if not settings.airtable_table_name:
            missing.append("AIRTABLE_TABLE_NAME")
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        print("Required Airtable settings are present.")
        print("Optional integrations:")
        print(f"  HappyScribe: {'yes' if settings.happyscribe_api_key else 'no'}")
        print(f"  Canva: {'yes' if canva_settings_complete(settings) else 'no'}")
        print(f"  YouTube: {'yes' if youtube_settings_complete(settings) else 'no'}")
        if settings.youtube_channel_handle:
            print(f"    Channel: @{settings.youtube_channel_handle}")
        print(f"  Meta: {'yes' if settings.meta_access_token else 'no'}")
        print(f"    Facebook: {meta_facebook_url(settings)}")
        print(f"    Instagram: {meta_instagram_url(settings)}")
        if settings.meta_access_token:
            print(f"    Page ID: {'set' if settings.meta_page_id else 'resolve via username'}")
            print(
                "    Instagram account ID: "
                f"{'set' if settings.meta_instagram_account_id else 'resolve via page link'}"
            )
        return 0

    if args.test_airtable:
        missing = []
        if not settings.airtable_token:
            missing.append("AIRTABLE_TOKEN")
        if not settings.airtable_base_id:
            missing.append("AIRTABLE_BASE_ID")
        if not settings.airtable_table_name:
            missing.append("AIRTABLE_TABLE_NAME")
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        try:
            client = airtable_client_from_settings(settings)
            count = client.test_connection(max_records=1)
        except AirtableError as exc:
            print(f"Airtable connection failed: {exc}")
            return 1
        print(
            f"Airtable connection OK ({settings.airtable_table_name!r}, "
            f"{count} record(s) sampled)."
        )
        return 0

    if args.test_happyscribe:
        missing = happyscribe_settings_missing(settings)
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        try:
            client = happyscribe_client_from_settings(settings)
            location = None
            try:
                location = happyscribe_library_from_settings(settings)
            except HappyScribeError:
                location = None
            org_id, count = client.test_connection(location)
        except HappyScribeError as exc:
            print(f"HappyScribe connection failed: {exc}")
            return 1
        if location is not None:
            print(
                "HappyScribe connection OK "
                f"(organization {org_id!r}, folder {location.folder_id!r}, "
                f"{count} transcription(s) sampled)."
            )
        else:
            print(
                f"HappyScribe connection OK (organization {org_id!r}, "
                f"{count} transcription(s) sampled)."
            )
        return 0

    if args.list_happyscribe_library:
        missing = happyscribe_library_settings_missing(settings)
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        try:
            client = happyscribe_client_from_settings(settings)
            location = happyscribe_library_from_settings(settings)
            transcriptions = client.list_library_transcriptions(location)
        except HappyScribeError as exc:
            print(f"HappyScribe library listing failed: {exc}")
            return 1
        if not transcriptions:
            print(
                f"No transcriptions found in folder {location.folder_id!r} "
                f"(organization {location.organization_id!r})."
            )
            return 0
        for transcription in transcriptions:
            print_console(
                f"{transcription.id}\t{transcription.state}\t{transcription.name}"
            )
        print_console(f"{len(transcriptions)} transcription(s) in library folder.")
        return 0

    if args.download_happyscribe_library:
        missing = happyscribe_library_settings_missing(settings)
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        download_dir = PROJECT_ROOT / settings.happyscribe_download_dir
        browser_state = happyscribe_browser_state_path(settings)
        try:
            client = happyscribe_client_from_settings(settings)
            location = happyscribe_library_from_settings(settings)
            transcriptions = client.list_library_transcriptions(location)
            downloaded = []
            for transcription in transcriptions:
                if transcription.state != TRANSCRIPTION_STATE_READY:
                    continue
                if is_subtitled_export_name(transcription.name):
                    continue
                destination = burned_video_destination_path(
                    download_dir,
                    transcription.name,
                )
                path = export_video_with_subtitles_web(
                    transcription.id,
                    destination,
                    **happyscribe_web_export_kwargs(settings, args),
                )
                downloaded.append(path)
                print_console(str(path))
        except (HappyScribeError, HappyScribeWebError) as exc:
            print(f"HappyScribe library download failed: {exc}")
            return 1
        if not downloaded:
            print(
                f"No ready videos to download in folder {location.folder_id!r} "
                f"(organization {location.organization_id!r})."
            )
            return 0
        print(f"Downloaded {len(downloaded)} web-exported video(s) to {download_dir}.")
        return 0

    if args.happyscribe_save_session:
        browser_state = happyscribe_browser_state_path(settings)
        try:
            save_browser_session_interactive(
                browser_state,
                browser_profile_dir=happyscribe_browser_profile_path(settings),
                email=settings.happyscribe_email,
                password=settings.happyscribe_password,
                browser_channel=settings.happyscribe_browser_channel,
            )
        except HappyScribeWebError as exc:
            print(f"HappyScribe session setup failed: {exc}")
            return 1
        print(f"Saved HappyScribe browser session to {settings.happyscribe_browser_state!r}.")
        return 0

    if args.happyscribe_import_session:
        browser_state = happyscribe_browser_state_path(settings)
        try:
            import_browser_session(Path(args.happyscribe_import_session), browser_state)
        except HappyScribeWebError as exc:
            print(f"HappyScribe session import failed: {exc}")
            return 1
        print(f"Imported HappyScribe browser session to {settings.happyscribe_browser_state!r}.")
        return 0

    if args.export_happyscribe_web:
        missing = happyscribe_web_settings_missing(settings)
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        download_dir = PROJECT_ROOT / settings.happyscribe_download_dir
        try:
            client = happyscribe_client_from_settings(settings)
            transcription = client.get_transcription(args.export_happyscribe_web)
            destination = burned_video_destination_path(download_dir, transcription.name)
            path = export_video_with_subtitles_web(
                args.export_happyscribe_web,
                destination,
                **happyscribe_web_export_kwargs(settings, args),
            )
        except (HappyScribeError, HappyScribeWebError) as exc:
            print(f"HappyScribe web export failed: {exc}")
            return 1
        print(f"Saved web-exported video to {path}")
        print(f"Size bytes: {path.stat().st_size}")
        return 0

    if args.burn_happyscribe_video:
        missing = happyscribe_settings_missing(settings)
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        download_dir = PROJECT_ROOT / settings.happyscribe_download_dir
        try:
            client = happyscribe_client_from_settings(settings)
            transcription = client.get_transcription(args.burn_happyscribe_video)
            destination = burned_video_destination_path(download_dir, transcription.name)
            path = client.download_video_with_burned_subtitles(
                args.burn_happyscribe_video,
                destination,
                work_dir=download_dir / ".work",
            )
        except HappyScribeError as exc:
            print(f"HappyScribe subtitle burn failed: {exc}")
            return 1
        print(f"Saved subtitled video to {path}")
        print(f"Size bytes: {path.stat().st_size}")
        return 0

    if args.canva_auth:
        missing = []
        if not settings.canva_client_id:
            missing.append("CANVA_CLIENT_ID")
        if not settings.canva_client_secret:
            missing.append("CANVA_CLIENT_SECRET")
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        try:
            client = canva_client_from_settings(settings)
            url = client.start_authorization()
        except CanvaError as exc:
            print(f"Canva authorization setup failed: {exc}")
            return 1
        print("Open this URL in a browser and authorize the integration:")
        print(url)
        print()
        print(
            "After approval, run:\n"
            f"  python -m media_publisher --canva-auth-code <authorization_code>"
        )
        return 0

    if args.canva_auth_code:
        missing = []
        if not settings.canva_client_id:
            missing.append("CANVA_CLIENT_ID")
        if not settings.canva_client_secret:
            missing.append("CANVA_CLIENT_SECRET")
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        try:
            client = canva_client_from_settings(settings)
            token = client.complete_authorization(
                args.canva_auth_code,
                state=args.canva_auth_state,
            )
        except CanvaError as exc:
            print(f"Canva authorization failed: {exc}")
            return 1
        print(f"Canva token saved to {settings.canva_token!r}.")
        if token.scope:
            print(f"Scopes: {token.scope}")
        return 0

    if args.canva_resolve:
        try:
            resolved_url = resolve_canva_url(args.canva_resolve)
            design_id = parse_design_id(resolved_url)
        except CanvaError as exc:
            print(f"Canva URL resolution failed: {exc}")
            return 1
        print(f"Resolved URL: {resolved_url}")
        print(f"Design ID: {design_id}")
        return 0

    if args.canva_download:
        missing = canva_settings_missing(settings)
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        download_dir = PROJECT_ROOT / settings.canva_download_dir
        try:
            downloaded = download_images_from_canva_url(
                args.canva_download,
                client_id=settings.canva_client_id or "",
                client_secret=settings.canva_client_secret or "",
                token_path=PROJECT_ROOT / settings.canva_token,
                download_dir=download_dir,
                api_base=settings.canva_api_base,
                redirect_uri=settings.canva_redirect_uri,
                export_format=args.canva_format,
            )
        except CanvaError as exc:
            print(f"Canva download failed: {exc}")
            return 1
        for path in downloaded:
            print(path)
        print(f"Downloaded {len(downloaded)} image(s) to {download_dir}.")
        return 0

    if args.test_canva:
        missing = canva_settings_missing(settings)
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        try:
            client = canva_client_from_settings(settings)
            token = client.test_connection()
        except CanvaError as exc:
            print(f"Canva connection failed: {exc}")
            return 1
        print("Canva connection OK (token refreshed).")
        if token.scope:
            print(f"Scopes: {token.scope}")
        return 0

    if args.youtube_auth:
        secrets_path = PROJECT_ROOT / settings.youtube_client_secrets
        if not secrets_path.exists():
            print(f"Missing required file: {settings.youtube_client_secrets}")
            return 1
        try:
            client = youtube_client_from_settings(settings)
            url = client.start_authorization()
        except YouTubePublishError as exc:
            print(f"YouTube authorization setup failed: {exc}")
            return 1
        print("Open this URL in a browser and authorize the integration:")
        print(url)
        print()
        print(
            f"Sign in with the Google account that manages "
            f"https://www.youtube.com/@{settings.youtube_channel_handle} "
            f"before approving access."
        )
        print()
        print(
            "After approval, copy the authorization code from the redirect URL and run:\n"
            f"  python -m media_publisher --youtube-auth-code <authorization_code>"
        )
        return 0

    if args.youtube_auth_code:
        secrets_path = PROJECT_ROOT / settings.youtube_client_secrets
        if not secrets_path.exists():
            print(f"Missing required file: {settings.youtube_client_secrets}")
            return 1
        try:
            client = youtube_client_from_settings(settings)
            token = client.complete_authorization(
                args.youtube_auth_code,
                state=args.youtube_auth_state,
            )
        except YouTubePublishError as exc:
            print(f"YouTube authorization failed: {exc}")
            return 1
        print(f"YouTube token saved to {settings.youtube_token!r}.")
        if token.scope:
            print(f"Scopes: {token.scope}")
        return 0

    if args.test_youtube:
        missing = []
        if not (PROJECT_ROOT / settings.youtube_client_secrets).exists():
            missing.append(f"client secrets ({settings.youtube_client_secrets})")
        if not (PROJECT_ROOT / settings.youtube_token).exists():
            missing.append(f"token file ({settings.youtube_token})")
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        try:
            client = youtube_client_from_settings(settings)
            token = client.test_connection()
            channel = client.verify_authorized_channel()
        except YouTubePublishError as exc:
            print(f"YouTube connection failed: {exc}")
            return 1
        print("YouTube connection OK (token refreshed).")
        print(f"Channel: {channel.title}")
        print(f"URL: {channel.url}")
        if token.scope:
            print(f"Scopes: {token.scope}")
        return 0


    if args.test_meta:
        missing = meta_settings_missing(settings)
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        try:
            page_id, instagram_account_id, page_info = resolve_meta_targets(settings)
        except MetaError as exc:
            print(f"Meta connection failed: {exc}")
            return 1
        print("Meta connection OK.")
        print(f"Facebook page: {page_info.name} ({meta_facebook_url(settings)})")
        print(f"Page ID: {page_id}")
        print(f"Instagram: @{page_info.instagram_username or settings.meta_instagram_username}")
        print(f"Instagram URL: {meta_instagram_url(settings)}")
        print(f"Instagram account ID: {instagram_account_id}")
        return 0

    if args.resolve_meta:
        missing = meta_settings_missing(settings)
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        try:
            page_id, instagram_account_id, page_info = resolve_meta_targets(settings)
        except MetaError as exc:
            print(f"Meta resolve failed: {exc}")
            return 1
        print("Add these to your .env (optional — usernames are resolved automatically):")
        print(f"META_PAGE_ID={page_id}")
        print(f"META_INSTAGRAM_ACCOUNT_ID={instagram_account_id}")
        print()
        print(f"Facebook: {page_info.name} ({meta_facebook_url(settings)})")
        print(
            "Instagram: "
            f"@{page_info.instagram_username or settings.meta_instagram_username} "
            f"({meta_instagram_url(settings)})"
        )
        return 0

    if args.schedule_facebook:
        missing = meta_settings_missing(settings)
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        try:
            page_id, _, page_info = resolve_meta_targets(settings)
            job, airtable = load_publish_job_from_airtable(settings, args.schedule_facebook)
            post_id = publish_to_facebook(
                job,
                page_id=page_id,
                access_token=settings.meta_access_token or "",
            )
            airtable.update_record(
                args.schedule_facebook,
                {FIELD_FACEBOOK_POST_ID: post_id},
            )
        except (AirtableError, FacebookPublishError, MetaError) as exc:
            print(f"Facebook scheduling failed: {exc}")
            return 1
        when = job.publish_at.isoformat() if job.publish_at else "now"
        print(
            f"Facebook video scheduled for {when} on {page_info.name} "
            f"(video id: {post_id})."
        )
        return 0

    if args.schedule_instagram:
        missing = meta_settings_missing(settings)
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        try:
            _, instagram_account_id, page_info = resolve_meta_targets(settings)
            job, airtable = load_publish_job_from_airtable(settings, args.schedule_instagram)
            if job.video_path and not job.video_url and not settings.meta_app_id:
                print(
                    "Missing META_APP_ID — required when uploading a local video file "
                    "to Instagram."
                )
                return 1
            media_id = publish_to_instagram(
                job,
                instagram_account_id=instagram_account_id,
                access_token=settings.meta_access_token or "",
                app_id=settings.meta_app_id,
            )
            airtable.update_record(
                args.schedule_instagram,
                {FIELD_INSTAGRAM_MEDIA_ID: media_id},
            )
        except (AirtableError, InstagramPublishError, MetaError) as exc:
            print(f"Instagram scheduling failed: {exc}")
            return 1
        when = job.publish_at.isoformat() if job.publish_at else "now"
        ig_handle = page_info.instagram_username or settings.meta_instagram_username
        print(
            f"Instagram Reel scheduled for {when} on @{ig_handle} "
            f"(media id: {media_id})."
        )
        return 0

    parser.error(
        "No action specified. Try --check-config, --test-airtable, "
        "--test-happyscribe, --list-happyscribe-library, --download-happyscribe-library, "
        "--happyscribe-save-session, --happyscribe-import-session, --export-happyscribe-web, --burn-happyscribe-video, "
        "--canva-auth, --canva-download, --canva-resolve, --test-canva, --youtube-auth, --test-youtube, "
        "--test-meta, --resolve-meta, --schedule-facebook, or --schedule-instagram"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
