import { ReactNode, memo } from 'react';
import { Card } from '@/components/ui/Card';
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from '@/components/ui/Table';
import { cn } from '@/utils/cn';

export interface Column<T> {
  key: string;
  title: string;
  render?: (item: T, index: number) => ReactNode;
  className?: string;
}

export interface DataTableProps<T> {
  columns: Column<T>[];
  data: T[];
  keyExtractor: (item: T) => string;
  emptyMessage?: string;
  className?: string;
}

function DataTableInner<T>({
  columns,
  data,
  keyExtractor,
  emptyMessage = '暂无数据',
  className,
}: DataTableProps<T>) {
  if (data.length === 0) {
    return (
      <Card className={cn('text-center py-12', className)}>
        <p className="text-text-secondary">{emptyMessage}</p>
      </Card>
    );
  }

  return (
    <Card className={className}>
      <Table>
        <TableHeader>
          <TableRow>
            {columns.map((col) => (
              <TableHead key={col.key} className={col.className}>
                {col.title}
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {data.map((item, index) => (
            <TableRow key={keyExtractor(item)}>
              {columns.map((col) => (
                <TableCell key={col.key} className={col.className}>
                  {col.render ? col.render(item, index) : (item as Record<string, unknown>)[col.key] as ReactNode}
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </Card>
  );
}

export const DataTable = memo(DataTableInner) as typeof DataTableInner;
