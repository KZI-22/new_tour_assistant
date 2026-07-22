"use client";

import {
  Children,
  isValidElement,
  useState,
  type ComponentPropsWithoutRef,
  type ReactNode,
} from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

const XHS_IMAGE_ALT_PREFIX = "小红书原帖图片 ";
const INITIAL_IMAGE_COUNT = 9;

type GalleryImage = {
  previewUrl: string;
  originalUrl: string;
  alt: string;
  label: string;
};

type AnchorProps = ComponentPropsWithoutRef<"a">;
type ImageProps = ComponentPropsWithoutRef<"img">;

function nonWhitespaceChildren(children: ReactNode): ReactNode[] {
  return Children.toArray(children).filter(
    (child) => typeof child !== "string" || child.trim().length > 0,
  );
}

function isXhsCdnUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return (
      url.protocol === "https:" &&
      url.hostname.toLowerCase().endsWith(".xhscdn.com") &&
      !url.username &&
      !url.password
    );
  } catch {
    return false;
  }
}

function galleryImage(node: ReactNode): GalleryImage | null {
  if (!isValidElement<AnchorProps>(node) || node.type !== "a") return null;
  const imageChildren = nonWhitespaceChildren(node.props.children);
  if (imageChildren.length !== 1) return null;

  const image = imageChildren[0];
  if (!isValidElement<ImageProps>(image) || image.type !== "img") return null;

  const originalUrl = node.props.href;
  const previewUrl = image.props.src;
  const alt = image.props.alt ?? "";
  if (
    typeof originalUrl !== "string" ||
    typeof previewUrl !== "string" ||
    !isXhsCdnUrl(originalUrl) ||
    !isXhsCdnUrl(previewUrl) ||
    !alt.startsWith(XHS_IMAGE_ALT_PREFIX)
  ) {
    return null;
  }

  return {
    previewUrl,
    originalUrl,
    alt,
    label: node.props.title?.trim() || alt.slice(XHS_IMAGE_ALT_PREFIX.length),
  };
}

function XhsImageGallery({ images }: { images: GalleryImage[] }) {
  const [expanded, setExpanded] = useState(false);
  const [failedPreviews, setFailedPreviews] = useState<ReadonlySet<string>>(
    () => new Set(),
  );
  const visibleImages = expanded ? images : images.slice(0, INITIAL_IMAGE_COUNT);

  const markPreviewFailed = (url: string) => {
    setFailedPreviews((current) => {
      const next = new Set(current);
      next.add(url);
      return next;
    });
  };

  return (
    <div className="xhs-image-gallery" role="group" aria-label="小红书原帖图片">
      {visibleImages.map((image, index) => (
        <a
          key={`${image.originalUrl}-${index}`}
          className="xhs-image-card"
          href={image.originalUrl}
          target="_blank"
          rel="noreferrer noopener"
          title={`${image.label}：点击查看原图`}
        >
          {failedPreviews.has(image.previewUrl) ? (
            <span className="xhs-image-fallback">
              预览图加载失败
              <small>点击尝试打开原图</small>
            </span>
          ) : (
            // The source is validated by the API and intentionally uses the original CDN.
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={image.previewUrl}
              alt={image.alt}
              loading="lazy"
              decoding="async"
              referrerPolicy="no-referrer"
              onError={() => markPreviewFailed(image.previewUrl)}
            />
          )}
          <span className="xhs-image-index">{image.label}</span>
        </a>
      ))}
      {images.length > INITIAL_IMAGE_COUNT ? (
        <button
          type="button"
          className="xhs-image-gallery-toggle"
          aria-expanded={expanded}
          onClick={() => setExpanded((current) => !current)}
        >
          {expanded ? "收起图片" : `展开其余 ${images.length - INITIAL_IMAGE_COUNT} 张`}
        </button>
      ) : null}
    </div>
  );
}

const markdownComponents: Components = {
  p({ node, children, ...props }) {
    void node;
    const meaningfulChildren = nonWhitespaceChildren(children);
    const images = meaningfulChildren.map(galleryImage);
    const galleryImages = images.filter((image): image is GalleryImage => image !== null);
    if (galleryImages.length > 0 && galleryImages.length === meaningfulChildren.length) {
      return <XhsImageGallery images={galleryImages} />;
    }
    return <p {...props}>{children}</p>;
  },
};

export function AssistantMarkdown({ content }: { content: string }) {
  return (
    <ReactMarkdown components={markdownComponents} remarkPlugins={[remarkGfm]}>
      {content}
    </ReactMarkdown>
  );
}
