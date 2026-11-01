"""图片调用契约, 协议入参和真实传输边界"""
from __future__ import annotations

import asyncio
import base64
from io import BytesIO
import json
from pathlib import Path
from zipfile import ZipFile

import httpx
from PIL import Image
import pytest

from satrap import (
    AsyncImageGen, CharacterPrompt, ImageGen, ImageGenError, ImageGenParams,
    ImageInput, NovelAIParams, PreciseReference, VibeEncoding, VibeReference,
)
from satrap.core.APICall.ImageCall.media import decode_image
from satrap.core.APICall.ImageCall.request import merge_params
from satrap.core.APICall.ImageCall.response import parse_response


def png(color="white", size=(32, 48)):
    """构造可验证的离线图片"""
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def encoded(data):
    return base64.b64encode(data).decode("ascii")


def make_client(handler, *, async_=False, **kwargs):
    """使用真实 HTTP 请求构造和内存传输, 不访问外部服务"""
    cls = AsyncImageGen if async_ else ImageGen
    client = cls("private-test-token", "https://provider.test/custom/image/?version=2", model="nai-diffusion-4-5-full",
                 api_type=kwargs.pop("api_type", "novelai"), **kwargs)
    old = client.client
    client.client = (httpx.AsyncClient if async_ else httpx.Client)(transport=httpx.MockTransport(handler))
    if async_:
        return client, old
    old.close()
    return client


@pytest.mark.parametrize("api_type", ["novelai", "openai_images", "seedream"])
@pytest.mark.parametrize("shape", ["openai", "novelai", "binary", "zip", "siliconflow"])
def test_complete_url_and_protocol_independent_response(api_type, shape):
    data = png()
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "GET":
            assert "authorization" not in request.headers
            return httpx.Response(200, content=data, headers={"content-type": "image/png"})
        assert str(request.url) == "https://provider.test/custom/image/?version=2"
        assert request.headers["authorization"] == "Bearer private-test-token"
        if shape == "openai":
            return httpx.Response(200, json={"data": [{"b64_json": encoded(data)}], "usage": {"output_tokens": 3}})
        if shape == "novelai":
            return httpx.Response(201, json={"images": [{"image": encoded(data), "seed": 0}]})
        if shape == "siliconflow":
            return httpx.Response(200, json={"images": [{"url": "https://cdn.test/picture"}], "seed": 42})
        if shape == "zip":
            output = BytesIO()
            with ZipFile(output, "w") as archive:
                archive.writestr("../../picture.png", data)
            return httpx.Response(201, content=output.getvalue(), headers={"content-type": "binary/octet-stream"})
        return httpx.Response(200, content=data, headers={"content-type": "image/png"})

    with make_client(handler, api_type=api_type) as client:
        response = client.generate("一只猫")
    assert response and response.images[0].data == data
    assert (response.images[0].width, response.images[0].height) == (32, 48)
    assert response.usage == ({"output_tokens": 3} if shape == "openai" else None)
    assert len(calls) == (2 if shape == "siliconflow" else 1)


def test_parameter_merge_list_clear_zero_and_native_conflict():
    character = CharacterPrompt(prompt="girl")
    defaults = NovelAIParams(seed=7, steps=23, negative_prompt="blur", characters=[character],
                             native={"parameters": {"future": 1, "other": 2}})
    override = NovelAIParams(seed=0, negative_prompt="", characters=[], native={"parameters": {"future": 3}})
    result = merge_params(defaults, override, "novelai")
    assert result.seed == 0 and result.steps == 23 and result.negative_prompt == "" and result.characters == []
    assert result.native == {"parameters": {"future": 3, "other": 2}}
    assert defaults.characters == [character] and defaults.native["parameters"]["future"] == 1
    with make_client(lambda request: pytest.fail("不应发起请求")) as client:
        with pytest.raises(ValueError, match="冲突"):
            client.generate("猫", params=NovelAIParams(native={"parameters": {"seed": 123}}))


def test_novelai_character_precise_and_style_references_are_aligned():
    data = png()
    source = ImageInput.from_bytes(data)
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        parameters = body["parameters"]
        assert body["input"] == "two girls in a park" and body["action"] == "generate"
        caption = parameters["v4_prompt"]["caption"]
        assert caption["char_captions"][0] == {"char_caption": "girl, blue hair", "centers": [{"x": 0.2, "y": 0.5}]}
        assert parameters["v4_negative_prompt"]["caption"]["char_captions"][0]["char_caption"] == "hat"
        assert parameters["director_reference_strength_values"] == [0.8, 0.5]
        assert parameters["director_reference_secondary_strength_values"] == [0.7, 1.0]
        assert [item["caption"]["base_caption"] for item in parameters["director_reference_descriptions"]] == ["character", "style"]
        reference = decode_image(base64.b64decode(parameters["director_reference_images"][0]))
        assert (reference.width, reference.height) == (1024, 1536)
        return httpx.Response(201, json={"images": [{"image": encoded(data)}]})

    params = NovelAIParams(seed=0, size=(832, 1216), characters=[CharacterPrompt(prompt="girl, blue hair", negative_prompt="hat", centers=[(0.2, 0.5)])],
                          use_coords=True, precise_references=[PreciseReference(image=source, strength=0.8, fidelity=0.7),
                                                              PreciseReference(image=source, kind="style", strength=0.5)])
    with make_client(handler) as client:
        response = client.generate("two girls in a park", params=params)
    assert response and len(calls) == 1


@pytest.mark.parametrize("masked", [False, True])
def test_novelai_edit_and_inpaint(masked):
    data = png()
    source = ImageInput.from_bytes(data)

    def handler(request):
        body = json.loads(request.content)
        assert body["action"] == ("infill" if masked else "img2img")
        assert base64.b64decode(body["parameters"]["image"]) == data
        if masked:
            assert body["parameters"]["img2img"] == {"strength": 0.4, "noise": 0.0}
            assert "mask" in body["parameters"]
        else:
            assert body["parameters"]["strength"] == 0.4
        return httpx.Response(201, content=data, headers={"content-type": "image/png"})

    with make_client(handler) as client:
        params = NovelAIParams(strength=0.4, model="nai-diffusion-4-5-full-inpainting" if masked else None)
        result = client.edit("blue coat", [source], mask=source if masked else None, params=params)
    assert result


def test_openai_edit_multipart_uses_explicit_address_and_transparent_mask():
    data = png()
    seen = []

    def handler(request):
        seen.append(request)
        assert str(request.url) == "https://provider.test/exact/edit?x=1"
        assert "multipart/form-data" in request.headers["content-type"]
        body = request.content
        assert b'name="image[]"' in body and b'name="mask"' in body and b'name="quality"' in body
        return httpx.Response(200, json={"data": [{"b64_json": encoded(data)}]})

    with make_client(handler, api_type="openai_images", edit_base_url="https://provider.test/exact/edit?x=1") as client:
        result = client.edit("blue coat", [ImageInput.from_bytes(data), ImageInput.from_bytes(data)],
                             mask=ImageInput.from_bytes(png("white")), params=ImageGenParams(native={"quality": "high"}))
    assert result and len(seen) == 1
    from satrap.core.APICall.ImageCall.media import mask_png
    with Image.open(BytesIO(mask_png(png("white"), transparent=True))) as mask:
        assert mask.getpixel((0, 0))[3] == 0


def test_seedream_edit_data_urls_and_native_fields():
    data = png()

    def handler(request):
        body = json.loads(request.content)
        assert body["size"] == "2K" and body["seed"] == 0
        assert len(body["image"]) == 2 and all(value.startswith("data:image/png;base64,") for value in body["image"])
        assert body["watermark"] is True
        return httpx.Response(200, json={"images": [{"image": encoded(data)}]})

    with make_client(handler, api_type="seedream") as client:
        assert client.edit("combine", [ImageInput.from_bytes(data)] * 2,
                           params=ImageGenParams(size="2K", seed=0, native={"watermark": True}))


@pytest.mark.parametrize("status", [301, 400, 401, 402, 429, 500])
def test_http_failure_does_not_retry_or_leak(status, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="private-test-token sensitive-prompt", headers={"location": "https://other.test"})

    with make_client(handler) as client:
        assert client.generate("sensitive-prompt") is None
    assert len(calls) == 1
    assert "private-test-token" not in caplog.text and "sensitive-prompt" not in caplog.text
    with make_client(handler, suppress_error=False) as client:
        with pytest.raises(ImageGenError) as error:
            client.generate("sensitive-prompt")
        assert error.value.status == status and "private-test-token" not in str(error.value)


@pytest.mark.parametrize("body", [{"error": {"message": "secret"}}, {"output": {"task_id": "1", "task_status": "PENDING"}}, {}, {"data": []}])
def test_error_pending_and_empty_responses_are_not_success(body):
    with make_client(lambda request: httpx.Response(200, json=body), suppress_error=False) as client:
        with pytest.raises(ImageGenError):
            client.generate("cat")


def test_sse_only_returns_final_images_and_propagates_errors():
    data = png()
    preview = png("red")
    stream = "\n\n".join([
        'event: intermediate\ndata: '+json.dumps({"image": encoded(preview)}),
        'event: final\ndata: '+json.dumps({"image": encoded(data), "seed": 10}),
    ])+"\n\n"
    result = parse_response(stream.encode(), "text/event-stream")
    assert len(result.entries) == 1 and result.entries[0].data == data
    broken = stream+'event: error\ndata: {"message":"private-test-token"}\n\n'
    with pytest.raises(ImageGenError, match="provider_error"):
        parse_response(broken.encode(), "text/event-stream")


@pytest.mark.parametrize("params", [NovelAIParams(n=0), NovelAIParams(seed=-1), NovelAIParams(steps=0),
                                       NovelAIParams(scale=float("nan")), NovelAIParams(size=(-1, 30)),
                                       NovelAIParams(output_format="jpeg")])
def test_invalid_parameters_never_send_requests(params):
    with make_client(lambda request: pytest.fail("不应发起请求")) as client:
        with pytest.raises(ValueError):
            client.generate("cat", params=params)


def test_vibe_is_explicit_reusable_and_incompatible_combinations_fail():
    data = png()
    encoding = b"encoded-vibe-binary"
    calls = []

    def handler(request):
        calls.append(str(request.url))
        body = json.loads(request.content)
        if request.url.path == "/encode":
            assert body["information_extracted"] == 0.7
            return httpx.Response(201, content=encoding)
        assert body["parameters"]["reference_image_multiple"] == [encoded(encoding)]
        assert body["parameters"]["reference_information_extracted_multiple"] == [0.7]
        return httpx.Response(201, json={"images": [{"image": encoded(data)}]})

    with make_client(handler) as client:
        vibe = client.encode_vibe(ImageInput.from_bytes(data), base_url="https://provider.test/encode", information_extracted=0.7)
        assert vibe and vibe.data == encoding
        assert client.generate("cat", params=NovelAIParams(vibes=[VibeReference(encoding=vibe, strength=0.5)]))
        with pytest.raises(ValueError, match="不能同时"):
            client.generate("cat", params=NovelAIParams(vibes=[VibeReference(encoding=vibe)],
                            precise_references=[PreciseReference(image=ImageInput.from_bytes(data))]))
    assert len(calls) == 2


def test_remote_reference_download_deduplicates_and_does_not_send_credentials():
    data = png()
    gets = []

    def handler(request):
        if request.method == "GET":
            gets.append(str(request.url))
            assert "authorization" not in request.headers
            if request.url.path == "/redirect":
                return httpx.Response(302, headers={"location": "/asset"})
            return httpx.Response(200, content=data)
        return httpx.Response(200, json={"images": [{"image": encoded(data)}]})

    source = ImageInput.from_url("https://cdn.test/redirect")
    with make_client(handler, api_type="seedream") as client:
        assert client.edit("combine", [source, source])
    assert gets == ["https://cdn.test/redirect", "https://cdn.test/asset"]


@pytest.mark.asyncio
async def test_async_generate_edit_vibe_and_lifecycle():
    data = png()
    bodies = []

    async def handler(request):
        bodies.append(json.loads(request.content))
        await asyncio.sleep(0)
        return httpx.Response(201, json={"images": [{"image": encoded(data)}]})

    client, old = make_client(handler, async_=True)
    await old.aclose()
    async with client:
        assert await client.generate("cat", params=NovelAIParams(seed=0))
        assert await client.edit("blue coat", [ImageInput.from_bytes(data)])
    assert client.client.is_closed
    assert [body["action"] for body in bodies] == ["generate", "img2img"]


@pytest.mark.asyncio
async def test_async_cancel_does_not_retry_or_suppress():
    called = asyncio.Event()
    requests = []

    async def handler(request):
        requests.append(request)
        called.set()
        await asyncio.Event().wait()

    client, old = make_client(handler, async_=True)
    await old.aclose()
    async with client:
        task = asyncio.create_task(client.generate("cat"))
        await called.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(requests) == 1


@pytest.mark.parametrize("url", ["https://example.com", "http://localhost:19870/exact/", "https://example.com/a?b=1"])
def test_url_validation_preserves_full_url(url):
    with ImageGen("key", url, api_type="openai_images", model="model") as client:
        assert client.base_url == url


@pytest.mark.parametrize("url", ["/images/generate", "ftp://example.com/a", "https://user:pass@example.com/a", "https://example.com/a#b", "http://example.com/a"])
def test_invalid_urls_are_rejected(url):
    with pytest.raises(ValueError):
        ImageGen("key", url, api_type="openai_images", model="model")


@pytest.mark.parametrize("invalid", [ImageInput.from_bytes(b"not-an-image"), ImageInput.from_bytes(png(), mime_type="image/jpeg"),
                                     ImageInput.from_path("missing-picture.png"), ImageInput(kind="path", value=b"wrong")])
def test_invalid_local_input_raises_even_when_api_errors_are_suppressed(invalid):
    with make_client(lambda request: pytest.fail("不应发起请求")) as client:
        with pytest.raises(ValueError):
            client.edit("cat", [invalid])


def test_output_order_metadata_and_duplicate_downloads():
    first, second = png("red"), png("blue")
    gets = []

    def handler(request):
        if request.method == "GET":
            gets.append(request)
            assert "authorization" not in request.headers
            return httpx.Response(200, content=second)
        return httpx.Response(200, json={"model": "actual-model", "request_id": "req_123", "usage": {"image_count": 3},
            "data": [{"b64_json": encoded(first), "revised_prompt": "red"}, {"url": "https://cdn.test/image", "seed": 1},
                     {"url": "https://cdn.test/image", "seed": 2}]})

    with make_client(handler) as client:
        result = client.generate("cat")
    assert result and [image.data for image in result.images] == [first, second, second]
    assert [image.seed for image in result.images] == [None, 1, 2]
    assert result.images[0].revised_prompt == "red" and result.model == "actual-model"
    assert result.request_id == "req_123" and result.usage == {"image_count": 3} and len(gets) == 1


@pytest.mark.parametrize("content", [b'{"images":[{"image":"!!!"}]}', b"not an image", b'{"data":[{"b64_json":"bm90IGFuIGltYWdl"}]}'])
def test_malformed_output_is_rejected(content):
    with pytest.raises(ImageGenError):
        parse_response(content, "application/json" if content.startswith(b"{") else "image/png")


def test_zip_image_count_limit():
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        for index in range(33):
            archive.writestr(f"{index}.png", png())
    with pytest.raises(ImageGenError, match="response_size_limit"):
        parse_response(output.getvalue(), "application/zip")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["http", "timeout", "parse"])
async def test_async_error_policy_and_no_retry(failure, caplog):
    calls = []

    async def handler(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private-test-token", request=request)
        if failure == "http":
            return httpx.Response(429, json={"error": "private-test-token"})
        return httpx.Response(200, json={"images": []})

    client, old = make_client(handler, async_=True)
    await old.aclose()
    async with client:
        assert await client.generate("cat") is None
    assert len(calls) == 1 and "private-test-token" not in caplog.text


@pytest.mark.asyncio
async def test_async_url_result_and_explicit_vibe_encoding():
    data = png()
    calls = []

    async def handler(request):
        calls.append(request)
        if request.method == "GET":
            assert "authorization" not in request.headers
            return httpx.Response(200, content=data)
        if request.url.path == "/encode":
            return httpx.Response(201, content=b"encoded-vibe")
        body = json.loads(request.content)
        assert body["parameters"]["reference_image_multiple"] == [encoded(b"encoded-vibe")]
        return httpx.Response(200, json={"images": [{"url": "https://cdn.test/picture"}]})

    client, old = make_client(handler, async_=True)
    await old.aclose()
    async with client:
        vibe = await client.encode_vibe(ImageInput.from_bytes(data), base_url="https://provider.test/encode")
        assert vibe
        result = await client.generate("cat", params=NovelAIParams(vibes=[VibeReference(encoding=vibe)]))
        assert result and result.images[0].data == data
    assert len(calls) == 3


def test_default_params_are_snapshotted_and_caller_lists_unchanged():
    characters = [CharacterPrompt(prompt="woman")]
    native = {"parameters": {"future_option": [1, 2]}}
    defaults = NovelAIParams(characters=characters, native=native)

    def handler(request):
        parameters = json.loads(request.content)["parameters"]
        assert len(parameters["v4_prompt"]["caption"]["char_captions"]) == 1
        assert parameters["future_option"] == [1, 2]
        return httpx.Response(200, content=png())

    with make_client(handler, default_params=defaults) as client:
        characters.clear()
        native["parameters"]["future_option"].append(3)
        assert client.generate("cat")


def test_invalid_provider_url_obeys_api_error_policy():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"images": [{"url": "http://cdn.test/unsafe"}]})

    with make_client(handler) as client:
        assert client.generate("cat") is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_async_image_processing_runs_off_event_loop(monkeypatch):
    import threading
    from satrap.core.APICall.ImageCall import async_ as module

    main_thread = threading.get_ident()
    original_build = module.build_request
    original_parse = module.parse_response
    observed = []

    def build(*args, **kwargs):
        observed.append(threading.get_ident())
        return original_build(*args, **kwargs)

    def parse(*args, **kwargs):
        observed.append(threading.get_ident())
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(module, "build_request", build)
    monkeypatch.setattr(module, "parse_response", parse)
    client, old = make_client(lambda request: httpx.Response(200, content=png()), async_=True)
    await old.aclose()
    async with client:
        assert await client.generate("cat")
    assert len(observed) == 2 and all(thread != main_thread for thread in observed)
