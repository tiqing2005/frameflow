import { ArrowLeft, ImagePlus } from 'lucide-react'
import { ImageGenerationStudio } from '../components/ImageGenerationStudio'
import { AppLink, navigate } from '../router'

export function ImageGenerationPage({ generationId }: { generationId?: string }) {
  const updateLocation = (nextGenerationId: string | null) => {
    const path = nextGenerationId
      ? `/assets/generate?generation=${encodeURIComponent(nextGenerationId)}`
      : '/assets/generate'
    void navigate(path, { replace: true })
  }

  return (
    <main className="page image-generation-page">
      <div className="page-back-row"><AppLink href="/assets" className="back-link"><ArrowLeft size={17} /> 返回素材库</AppLink></div>
      <div className="page-heading-row compact-heading generation-page-heading">
        <div><span className="eyebrow"><ImagePlus size={14} /> 素材创作</span><h1>生成图片</h1><p>用自然语言创建一张可预览、可追踪的图片，确认后再加入素材库。</p></div>
        <span className="generation-one-image-note">每次生成 1 张</span>
      </div>
      <ImageGenerationStudio
        preferredGenerationId={generationId}
        storageKey="frameflow:image-generation:library"
        onGenerationChange={updateLocation}
      />
    </main>
  )
}
