import { memo } from 'react';
import { Modal } from '@/components/ui/Modal';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';

export interface FormField {
  key: string;
  label: string;
  type?: 'text' | 'password' | 'number' | 'textarea' | 'select' | 'checkbox';
  placeholder?: string;
  required?: boolean;
  disabled?: boolean;
  options?: { value: string; label: string }[];
  rows?: number;
}

export interface FormModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  fields: FormField[];
  values: Record<string, unknown>;
  onChange: (key: string, value: unknown) => void;
  onSubmit: () => void;
  submitText?: string;
  cancelText?: string;
  loading?: boolean;
  size?: 'sm' | 'md' | 'lg' | 'xl';
}

export const FormModal = memo(function FormModal({
  open,
  onClose,
  title,
  fields,
  values,
  onChange,
  onSubmit,
  submitText = '保存',
  cancelText = '取消',
  loading = false,
  size = 'md',
}: FormModalProps) {
  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    onSubmit();
  };

  const renderField = (field: FormField) => {
    const value = values[field.key];

    switch (field.type) {
      case 'textarea':
        return (
          <textarea
            value={(value as string) || ''}
            onChange={(e) => onChange(field.key, e.target.value)}
            placeholder={field.placeholder}
            disabled={field.disabled}
            rows={field.rows || 4}
            className="glass-input w-full font-mono text-sm resize-none"
          />
        );
      case 'select':
        return (
          <select
            value={(value as string) || ''}
            onChange={(e) => onChange(field.key, e.target.value)}
            disabled={field.disabled}
            className="glass-input w-full"
          >
            {field.options?.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        );
      case 'number':
        return (
          <Input
            type="number"
            step="any"
            value={(value as number) ?? ''}
            onChange={(e) => onChange(field.key, e.target.value ? Number(e.target.value) : undefined)}
            placeholder={field.placeholder}
            disabled={field.disabled}
          />
        );
      case 'checkbox':
        return (
          <label className="flex cursor-pointer items-center gap-3 rounded-sm bg-glass px-3 py-2">
            <input
              type="checkbox"
              checked={Boolean(value)}
              onChange={(e) => onChange(field.key, e.target.checked)}
              disabled={field.disabled}
              className="h-4 w-4 accent-accent"
            />
            <span className="text-sm text-text-primary">{field.placeholder || '启用'}</span>
          </label>
        );
      case 'password':
        return (
          <Input
            type="password"
            value={(value as string) || ''}
            onChange={(e) => onChange(field.key, e.target.value)}
            placeholder={field.placeholder}
            disabled={field.disabled}
          />
        );
      default:
        return (
          <Input
            type="text"
            value={(value as string) || ''}
            onChange={(e) => onChange(field.key, e.target.value)}
            placeholder={field.placeholder}
            disabled={field.disabled}
            required={field.required}
          />
        );
    }
  };

  return (
    <Modal open={open} onClose={onClose} title={title} size={size}>
      <form onSubmit={handleSubmit} className="space-y-4">
        {fields.map((field) => (
          <div key={field.key}>
            <label className="block text-sm font-medium text-text-secondary mb-1">
              {field.label}
              {field.required && <span className="text-error ml-1">*</span>}
            </label>
            {renderField(field)}
          </div>
        ))}

        <div className="flex gap-3 pt-4">
          <Button type="submit" variant="primary" className="flex-1" disabled={loading}>
            {loading ? '处理中...' : submitText}
          </Button>
          <Button type="button" variant="default" onClick={onClose}>
            {cancelText}
          </Button>
        </div>
      </form>
    </Modal>
  );
});
