import { useEffect, useRef, useState } from 'react';
import { Button } from '@/components/kit';
import { Modal } from '@/components/ui/Modal';
import { controlApi, type ImgTestResult } from '@/api/control';

export function ImgTestModal({ name, onClose }: { name: string | null; onClose: () => void }) {
  const [prompt, setPrompt] = useState('a cat sitting by a window');
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<ImgTestResult | null>(null);
  const [error, setError] = useState('');
  const sequence = useRef(0);
  useEffect(() => {
    sequence.current++;
    setResult(null); setError(''); setRunning(false);
  }, [name]);
  const run = async () => {
    if (!name || running || !prompt.trim()) return;
    const request = ++sequence.current;
    setRunning(true); setError(''); setResult(null);
    try {
      const response = await controlApi.testImgConfig(name, prompt.trim());
      if (request !== sequence.current) return;
      if (response.ok === false) setError(response.error || '图片生成失败');
      else setResult(response);
    } catch (caught) {
      if (request === sequence.current) setError(caught instanceof Error ? caught.message : '图片生成失败');
    } finally {
      if (request === sequence.current) setRunning(false);
    }
  };
  return <Modal open={name !== null} onClose={onClose} title={`测试图片生成: ${name ?? ''}`} size="lg">
    <div className="space-y-4">
      <p className="text-sm text-text-secondary">使用已保存的模型配置生成一张测试图片，会产生一次真实绘图调用。</p>
      <label className="block space-y-1 text-sm">
        <span>绘图提示词</span>
        <textarea className="glass-input w-full" aria-label="绘图提示词" rows={3} maxLength={4000}
          value={prompt} onChange={(event) => setPrompt(event.target.value)} />
      </label>
      {error && <p role="alert" className="text-error">{error}</p>}
      {result && <div role="status" className="space-y-3">
        <p className="text-xs text-text-tertiary">模型 {result.model} · 耗时 {result.elapsed_ms ?? '—'} ms</p>
        {result.images?.map((image, index) => <img key={index} src={image.data_url} alt={`测试生成图片 ${index + 1}`}
          className="mx-auto max-h-[55vh] max-w-full rounded-lg object-contain" />)}
      </div>}
      <div className="flex justify-end gap-2">
        <Button onClick={onClose}>关闭</Button>
        <Button variant="primary" disabled={running || !prompt.trim()} onClick={() => void run()}>{running ? '生成中…' : '开始生成'}</Button>
      </div>
    </div>
  </Modal>;
}
