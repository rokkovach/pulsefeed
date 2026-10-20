"""Only contract-approved image decoders may inspect provider bytes."""
import base64
import io

import pytest
from PIL import Image, PngImagePlugin

from pulsefeed.errors import AppError
from pulsefeed.provider import validate_envelope


async def test_unsupported_image_format_never_invokes_its_decoder(env,monkeypatch):
    request = {'schema_version':1,'stage':'flux','run_id':'format-run','invocation_id':'format-invocation',
               'deadline_unix_ms':9999999999999,'prompt':'a garden','seed':2,'width':1024,'height':1024,
               'num_inference_steps':4,'guidance_scale':1.0,'output_format':'webp'}
    job = await env['provider'].submit('flux',request)
    output = (await env['provider'].status('flux',job))['output']['result']
    image = io.BytesIO()
    Image.new('RGB',(1024,1024)).save(image,format='PNG')
    raw = image.getvalue()
    output['data'].update(image_base64=base64.b64encode(raw).decode(),image_bytes=len(raw))
    def forbidden_decoder(*args,**kwargs):
        raise AssertionError('A non-contract decoder inspected provider bytes')
    monkeypatch.setattr(PngImagePlugin.PngImageFile,'_open',forbidden_decoder)
    with pytest.raises(AppError) as error:
        validate_envelope(output,{'stage':'flux','run_id':request['run_id'],'id':request['invocation_id']},request)
    assert error.value.code == 'INVALID_MODEL_OUTPUT'
